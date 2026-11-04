# turn-reminder

可畏（Hermes）在 **Telegram / Discord** 每一轮都附一小段大白话约定的 Hermes 插件。

- 钩子：`pre_llm_call`，返回 `{"context": REMINDER}`。
- 注入位置：最新一条 **user 消息末尾**（`agent/turn_context.py:793 _collect_pre_llm_call_context`
  → `:1284-1299 compose_user_api_content`，只改本轮 API 视图），**不碰 system prompt**，
  所以 system + 历史前缀字节不变，不破前缀缓存。
- 只管 `platform` 为 `telegram` / `discord` 的轮次；`cron`、`weixin`、`desktop`、`acp`、
  `subagent`、空平台，以及 assistant / tongxi profile 的会话一律返回 `None`。
- 任何异常都放行（fail-open），不会挡住入站。

## 部署

```bat
:: 把本目录整体复制到 Hermes 插件目录
xcopy /E /I tools\hermes\turn-reminder "%LOCALAPPDATA%\hermes\plugins\turn-reminder"
```

然后在 `%LOCALAPPDATA%\hermes\config.yaml` 的 `plugins.enabled` 下加一行：

```yaml
plugins:
  enabled:
    - turn-reminder
```

重启网关（`hermes gateway stop`，再等计划任务「Hermes Gateway Watchdog」拉起），
`gateway.log` 里能看到插件加载。

## 自检

```bat
python tools\hermes\turn-reminder\__init__.py --selftest
```

断言：telegram / discord 返回提醒原文；cron、weixin、desktop、acp、subagent、空平台、
assistant/tongxi profile 都返回 `None`；平台枚举值也能识别。

## 原文（一字不改）

```
（每轮提醒，只给我自己看：说话用现在的人平常说的大白话，不用「便是」「罢了」「歇息」这类古旧字眼；不用 emoji，也不用颜文字；闲聊像发消息那样连着写，想分开发就在两条之间单独写一行 [分条]；没什么想说的时候整条只写 [SILENT]，指挥官问我事情、交代我办事时一定回答。）
```
