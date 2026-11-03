# Hermes（可畏）侧补丁

这里放**部署到 Windows 主 Hermes** 的脚本与配置说明。仓库不保存 Hermes 的私人
`config.yaml` / `.env`（含 token），只放脚本和「要改哪几行」的说明。

## `discord_whisper_budget.py`（2026-11-02）

Discord「悄悄话」频道的条数额度 hook：和现有的 `discord_group_budget.py` 同一套写法，
两个 hook 都挂在 `pre_llm_call` / `transform_llm_output` 上，各自只认自己的频道：

| 频道 | hook | 额度来源 |
|---|---|---|
| #常规（群） | `scripts/discord_group_budget.py`（已部署，不改） | 阿尔比恩 bridge `GET /group_budget` |
| #悄悄话 | `scripts/discord_whisper_budget.py`（本文件） | 阿尔比恩 bridge `GET /whisper_budget` |

行为：

- `pre_llm_call`：把 bridge 回话里的 `note` 注入给可畏（剩 4 条「该收尾了」/ 剩 0「聊到头了」）；
- `transform_llm_output`：`remaining=0` 时这一轮改成 `[SILENT]`（硬拦）；
- `remaining>0` 且没有 note 时什么都不插；bridge 连不上（阿尔比恩没起）直接放行，fail-open；
- 别的频道 / 私信：输出 `{}`，一个字都不改。

### 部署（主脑另派，这里只是清单）

1. 把 `tools/hermes/discord_whisper_budget.py` 复制到
   `%LOCALAPPDATA%\hermes\scripts\discord_whisper_budget.py`。
2. `%LOCALAPPDATA%\hermes\config.yaml` 的 `hooks` 两段各加一条（和 group 那条并列）：

   ```yaml
   hooks:
     pre_llm_call:
       - command: 'C:/Users/24346/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe "C:/Users/24346/AppData/Local/hermes/scripts/discord_group_budget.py"'
         timeout: 15
       - command: 'C:/Users/24346/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe "C:/Users/24346/AppData/Local/hermes/scripts/discord_whisper_budget.py"'
         timeout: 15
     transform_llm_output:
       - command: 'C:/Users/24346/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe "C:/Users/24346/AppData/Local/hermes/scripts/discord_group_budget.py"'
         timeout: 15
       - command: 'C:/Users/24346/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe "C:/Users/24346/AppData/Local/hermes/scripts/discord_whisper_budget.py"'
         timeout: 15
   ```

3. `%LOCALAPPDATA%\hermes\.env` 加 / 改：

   - `ALBION_DISCORD_WHISPER_CHANNEL_ID=<#悄悄话 的频道 id>`（和阿尔比恩 `/root/albion-pi/.env`
     里填的是同一个；不想动 Hermes 就用 `HERMES_WHISPER_CHANNEL_ID`，优先级更高）；
   - `DISCORD_FREE_RESPONSE_CHANNELS`：在现有的逗号列表里加上这个频道 id（不加的话可畏在
     #悄悄话 里不会自由回话）；
   - `ALBION_CHAT_KEY`：group hook 已经在用（= 阿尔比恩 `agent/.env` 的 `API_SERVER_KEY`），
     这里复用同一个，不用新增。

4. 重启 Windows Hermes（gateway / 桌面端），让它重读 `config.yaml` 与 `.env`。

### 部署前后怎么验

```bat
python "%LOCALAPPDATA%\hermes\scripts\discord_whisper_budget.py" --selftest
:: 桥在、频道 id 也配对了才会回 context；连不上桥一律输出 {}
echo {"hook_event_name":"pre_llm_call","channel_id":"<频道id>"} | python "%LOCALAPPDATA%\hermes\scripts\discord_whisper_budget.py"
```

阿尔比恩那侧（同一任务里改的仓库代码）部署后：

```bash
curl -s -H "Authorization: Bearer $API_SERVER_KEY" http://127.0.0.1:8660/whisper_budget
# -> {"remaining":…,"session_used":…,"day_used":…,"limit":…,"ok":true,"note":"…"}
curl -s http://127.0.0.1:8660/health   # whisper 字段＝频道 id 或 off
```

找不到频道时 bridge 只记一条 `whisper_channel_missing`，整块悄悄话不启用，私信 / 群 /
其它功能都不受影响。
