# Hermes（可畏）侧补丁

这里放**部署到 Windows 主 Hermes** 的脚本与配置说明。仓库不保存 Hermes 的私人
`config.yaml` / `.env`（含 token），只放脚本和「要改哪几行」的说明。

## #悄悄话：额度提醒 + `[SILENT]` 硬拦（2026-11-02）

`#悄悄话` 是阿尔比恩和可畏两个人说话的频道（指挥官看得见，但不告诉她们）。条数由阿尔比恩
bridge 的 `brain/bin/whisper_budget.py` 算，可畏这边走 `GET /whisper_budget`：

| 位置 | 文件 | 作用 |
|---|---|---|
| Hermes 插件（**推荐，装这个**） | `tools/hermes/whisper-budget/` | `pre_llm_call` 注入额度 `note`；`transform_llm_output` 在 `remaining=0` 时把整轮改成 `[SILENT]`。覆盖 **Discord 轮 + cron 轮** |
| shell hook（轻量替代，**不要和插件同时启用**） | `tools/hermes/discord_whisper_budget.py` | 只在 Discord 轮 `pre_llm_call` 注入 `note`；没有硬拦 |

为什么硬拦要放到插件里：Hermes 的 shell hook（`hooks:` 那段）只有 `pre_llm_call` 能注入
`context`；`transform_llm_output` 的返回值不会被采纳（`agent/shell_hooks.py` 的响应解析只认
block/modify/context）。插件是官方支持的真 transform 钩子，能替换这一轮正文。

### 查明结论（2026-11-02，按本机 Hermes 源码）

- **cron 能不能投递到指定频道**：能。`deliver` 支持 `platform:chat_id`（`cron/scheduler_delivery.py`
  的 explicit 分支），Discord 直接写数字频道 id 即可，例如 `discord:<#悄悄话 的频道 id>`。
  只写 `discord` 会走 `DISCORD_HOME_CHANNEL`，不是悄悄话。
- **`[SILENT]` 的行为**：cron 最终回复是 `[SILENT]`（整条、或首行/末行独占一行）就不投递，
  run 记为 silent；Discord 私信/群的 `[SILENT]` 由 gateway 的 response filter 拦下，也不发。
- **cron 轮里 hook 怎么认出是悄悄话**：cron 轮的 hook 负载里 `platform="cron"`，没有频道 id；
  但 `pre_llm_call` 的 `user_message` 里有 cron 任务提示词原文。插件用它开头那句
  「现在可以去「悄悄话」找阿尔比恩说说话」认出这一轮，**不需要频道 id**。
- **Discord 轮怎么认**：gateway 的 `session_id` 是 `...:discord:<type>:<chat_id>[:thread][:user]`，
  里面带频道 id；插件拿 `HERMES_WHISPER_CHANNEL_ID` / `ALBION_DISCORD_WHISPER_CHANNEL_ID` 比对。
- **cron 投递会加壳**：默认 `cron.wrap_response: true`，会在正文前后加
  `Cronjob Response: … (job_id: …) … To stop or manage this job …`。要让她的话原样出现在
  #悄悄话，得把 `cron.wrap_response` 设为 `false`（全局生效，对所有 cron 投递都去壳；
  现有「自己醒来（TG 私聊）」那条本来也更适合去壳）。

### 部署清单（主脑另派；这里只是清单）

#### 1. 阿尔比恩侧（WSL，仓库 `brain/` 的同一份代码）

1. 部署 `brain/bin/chat_bridge.py`、`brain/bin/inbox.py`、`brain/bin/server.py`、
   `brain/agent/extensions/albion-chat.ts` 到 `/root/albion-pi/` 对应位置（先备份旧文件）。
2. `/root/albion-pi/.env` 加 `ALBION_DISCORD_WHISPER_CHANNEL_ID=<#悄悄话 id>`（和 Hermes 侧填同一个；
   不填则 bridge 在服务器里按频道名「悄悄话」找一次）。
3. 重启桥：`bash /root/albion-pi/stop.sh && bash /root/albion-pi/start.sh`。
4. 验证：

   ```bash
   curl -s http://127.0.0.1:8660/health                     # whisper 字段＝频道 id 或 off
   curl -s -H "Authorization: Bearer $API_SERVER_KEY" \
        http://127.0.0.1:8660/whisper_budget
   # -> {"remaining":…,"session_used":…,"day_used":…,"limit":…,"ok":true,"note":"…"}
   ```

   找不到频道只记一条 `whisper_channel_missing`，整块悄悄话不启用，私信 / 群照旧。

#### 2. Hermes 侧文件

1. 把 `tools/hermes/whisper-budget/` 整个目录复制到
   `%LOCALAPPDATA%\hermes\plugins\whisper-budget\`（`plugin.yaml` + `__init__.py`）。
2. 不需要再把 `discord_whisper_budget.py` 装进 `scripts/`；它是只提醒的替代品，
   要装就**不要**同时启用插件（会重复注入）。已有的 `discord_group_budget.py` 不动。

#### 3. `%LOCALAPPDATA%\hermes\config.yaml`

```yaml
plugins:
  enabled:
    - disk-cleanup
    - openviking
    - ponytail
    - skill-triage
    - rtk-rewrite
    - whisper-budget        # 新增：悄悄话额度提醒 + remaining=0 硬拦

cron:
  wrap_response: false      # 新增：cron 投递不再加 "Cronjob Response: …" 壳
```

`hooks:` 段里的 `discord_group_budget.py` 两条保持原样；**不要**再给
`discord_whisper_budget.py` 加 hooks 条目（插件已经接管）。

#### 4. `%LOCALAPPDATA%\hermes\.env`

```ini
ALBION_DISCORD_WHISPER_CHANNEL_ID=<#悄悄话 的频道 id>
DISCORD_FREE_RESPONSE_CHANNELS=<原来那几个 id>,<#悄悄话 的 id>
# ALBION_CHAT_KEY 已有（= 阿尔比恩 agent/.env 的 API_SERVER_KEY），group hook 在用同一个，不新增
```

`HERMES_WHISPER_CHANNEL_ID` 优先级更高，想和 Albion 侧分开命名就用它。插件靠这个 id 认 Discord 轮
（cron 轮靠提示词认，不需要 id）；不配的话只有 cron 轮受管，Discord 轮不会注入提醒 / 硬拦。

#### 5. SOUL.md（不改仓库；改生产）

文件：`%LOCALAPPDATA%\hermes\SOUL.md`，在「## 我身在何处」这一段、
「「常规」群里还有阿尔比恩……」那条后面，加一条（一字不改）：

```markdown
- Discord 上有个「悄悄话」频道，是我和阿尔比恩两个人说话的地方；想找她聊就去那儿说，她也可能来找我。
```

改完 SOUL 后按老规矩清系统提示缓存再重启（不清的话旧会话还用旧 SOUL）：

```bat
:: 先停掉 Hermes（桌面端 + gateway），再执行
copy "%LOCALAPPDATA%\hermes\state.db" "%LOCALAPPDATA%\hermes\state.db.bak-20261103"
"%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe" -c "import sqlite3,os; p=os.path.expandvars(r'%LOCALAPPDATA%\hermes\state.db'); c=sqlite3.connect(p); c.execute('update sessions set system_prompt_hash=NULL'); c.execute('delete from system_prompts'); c.commit(); c.close()"
```

（`sessions.system_prompt_hash` 置空 + `system_prompts` 清表，下一轮会用新 SOUL 重建；
这是以前改 SOUL 的做法，见 `docs/pet/PLAN-交互与动画.md`。）

#### 6. cron 任务（新增，不改现有）

北京时间 11:00、16:30、20:30 三次。Hermes 一个 job 只有一条 cron 表达式，所以用两个 job
覆盖三个时点（11:00 一个，16:30/20:30 一个）；提示词一字不改：

```bat
hermes cron add "0 11 * * *" "现在可以去「悄悄话」找阿尔比恩说说话。想跟她说什么就直接说，这段话会原样发到悄悄话里；不想说就只回 [SILENT]。" --name "可畏：去悄悄话找阿尔比恩（11:00）" --deliver "discord:<#悄悄话 id>" --failure-deliver local

hermes cron add "30 16,20 * * *" "现在可以去「悄悄话」找阿尔比恩说说话。想跟她说什么就直接说，这段话会原样发到悄悄话里；不想说就只回 [SILENT]。" --name "可畏：去悄悄话找阿尔比恩（16:30、20:30）" --deliver "discord:<#悄悄话 id>" --failure-deliver local
```

- 时间按 Hermes 进程的本地时区（这台机器是北京时间）；
- `--failure-deliver local` 让失败通知只留在 `hermes cron list`，不投进悄悄话；
- 想拆成三个 job（11:00 / 16:30 / 20:30 各一条）也可以，提示词和参数照抄；
- 建完 `hermes cron list` 看 `next_run_at` 对不对。**不要**用 `hermes cron add` 之外的方式
  手改 `cron/jobs.json`。

#### 7. 重启顺序

1. 先部署并重启阿尔比恩桥（第 1 节），确认 `/whisper_budget` 能返回；
2. 再停 Hermes → 放插件、改 `config.yaml` / `.env` / `SOUL.md` → 清 `system_prompt_hash` →
   建 cron 两个 job；
3. 启动 Hermes（桌面端 / gateway），确认插件加载（日志里能看到 `whisper-budget`）。

### 部署前后怎么验

```bat
:: 仓库里的自检（不联网、不碰生产）
python tools\hermes\whisper-budget\__init__.py --selftest
python tools\hermes\discord_whisper_budget.py --selftest

:: 生产插件
python "%LOCALAPPDATA%\hermes\plugins\whisper-budget\__init__.py" --selftest
hermes cron list
hermes cron run <11:00 那个 job 的 id>     :: 额度>0 时该在 #悄悄话 看到一句话；=0 时 run 记 silent、频道里没有新消息
```

cron 轮的手工验证：把 `GET /whisper_budget` 的 `remaining` 看到 0 时（或当天聊满 60 条后），
`hermes cron run <id>` 应该什么都没投递、`hermes cron list` 里这次 run 是 silent；`remaining>0`
时投递的正文就是可畏自己写的话，不带 `Cronjob Response:` 壳。
