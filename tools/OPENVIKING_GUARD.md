# Windows OpenViking 守护

维护源在本目录，现网部署目录仍是 `D:/AI/scripts/`。只维护 OpenViking、守护锁与通知；不启用 llama 8082，不改 TTS 或模型配置。现有 `tailscale-guard-runner.vbs`、计划任务和登录入口保持原位。

- `tailscale-guard.cmd`：先 Ensure，再采样；8082 分支显式停用。保留原 CMD 编码和 CRLF。
- `tailscale-guard-conpty.pyw`：文件锁覆盖实际 CMD 生命周期；观察器与通知器用无继承管道的隐藏进程启动。CMD 超过 600 秒时记录并继续持锁等待，不杀可能持有 1933 的进程树。
- `openviking-memory-health.py`：查询任务并更新三个固定事件槽，不调用 Hermes。请求共享8秒预算，独立采样子进程再受10秒硬上限约束，防止缓慢响应绕过socket超时。复用现有私有 ov.conf 凭据，不嵌入凭据。
- `openviking-notify.py`：单消费者、尝试前节流、30 秒硬超时。发送前复核可达性和最新状态；用观察时间表述消息，避免把发送中的竞态描述为当前故障。2026-10-21 起改用 `hermes send` 直投用户本人的 Telegram 私信与微信私信（`TARGETS`），两路各自独立发送、独立记日志：不经模型、不占额度，退出码即平台回执。旧的 `hermes chat --resume` 路径把消息写进只有 Hermes 桌面端渲染的历史会话，同席与壳都看不到，等于没送达。
- `openviking_runtime.py`：原子状态、文件锁、日志轮转、Windows Job 包含子进程。通知 CLI 在挂入 kill-on-close Job 后才恢复运行；超时或 worker 退出只清理这个 Job 的子树。pywin32 不可用或无法分配 Job 时拒绝发送并节流，不降级为失控后台进程。
- `openviking-recover.ps1`：原恢复语义，补 run/Mode/Caller/父PID/阶段/耗时和启动早退退出码。
- `openviking-observe.ps1`：独立命名互斥锁；持有已验证服务进程句柄，记录启动身份和退出码。每 10 秒重扫、每秒观察退出、每 5 分钟心跳，日志按 5MB 轮转。短于扫描周期的进程可能漏记，观察器停机期间的退出无法补录。退出码可缩小故障类型，不能单独判定是谁结束进程。

状态在 `D:/AI/scripts/logs/.openviking-notify-state.json`，锁文件同目录；日志是 `openviking-health.log`、`openviking-notify.log`、`tailscale-guard.log` 和 `openviking-recovery/`。锁文件存在不代表锁被持有；以 OS 文件锁/命名互斥为准。不要删活跃锁文件。坏状态文件会拒绝发送并记错误，先留证再人工修复；不要直接清空状态规避节流。

部署前逐文件备份并保存 MD5；同时部署 runtime 和入口，guard CMD 最后切换，避免新旧调用链混用。先等旧轮次自然结束，不杀服务。健康运行时不必重启 1933。回滚使用本批备份恢复原文件；停用新增 worker 后再切旧通知入口，保留新状态和日志。

隔离测试与在线验收边界见 `tests/openviking/README.md`。现场备份、精确回滚命令、部署验证结果在主树忽略目录 `tests/output/ov-leftovers/IMPL.md`，私人产物不提交。

2026-10-21：采样器从观察器的当前/上一份轮转日志各读取最多5MB，只接纳最近5分钟的已核实OV进程退出（排除conhost），聚合到第三个有界`process`事件槽。即使Ensure已先恢复健康，也发送带`health_now`的历史退出通知；不把已恢复的退出事件当成仍不可达。成功后按最新退出时间去重，失败沿用30分钟节流，不改模型、凭据和原通知会话。此前只在不可达时采样会漏掉恢复前的短暂退出。退出码仍不能独立证明退出发起者。

2026-10-21：告警送达改走 `hermes send --to telegram`。旧命令 `hermes chat --resume 20260906_004558_d1a062` 把通知写进一个 source=desktop 的历史会话，只有 Hermes 桌面端会渲染；同席（ACP 会话源排除 desktop）和壳的聊天 UI 都不读它，CLI 退出 0 也只证明正文生成并落库，不代表用户看见。且每条告警都要跑一次模型，额度耗尽时必然撞 20 秒硬限（实测 7 次投递 0 次 exit 0，其中 4 次 124 超时）。改为直投 Telegram 私信后，生产实测 10:43:51 UTC 真实告警 exit=0，平台回执 message_id 123，正文落在 telegram DM 会话内。

2026-10-21：告警改为 Telegram 与微信两路并投（`TARGETS = ('telegram', 'weixin')`；`weixin` 即 `WEIXIN_HOME_CHANNEL`）。每路单独 `hermes send`、单独记日志（`... target=<平台> exit=<码>`），一路失败或超时不影响另一路。状态按路记在 `delivered_to`：已送达的一路不会因另一路失败而重发，只补发未送达的那路；旧状态里只有 telegram 的 `delivered_fail_ts` 迁移为 telegram 已送达，避免重发。生产部署前备份：`backup/code/2026-10-21/ov-alert-wechat/`。隔离端到端（测试 incident `e2e-test-*`，走已部署脚本）两路 exit=0，会话库确认 telegram dm 与 weixin dm 各落一条。

2026-10-21 结论（用户确认，不再深挖）：OV 提取失败（extraction failures=12）归因为两个 key 额度不足，不再排查 VLM 账号；历史进程退出的根因同样不再深挖。

同批还修正了真实恢复时暴露的输出管道继承：runner改用临时磁盘句柄接收CMD输出，并只等待CMD进程本身，最多读取1MiB诊断。不再用communicate等待全部继承者关闭stdout，避免OV已健康而guard仍持锁、后续每轮永久skip。测试让自有后台子进程保持stdout句柄，验证父轮次结束时子进程仍存活；生产部署只结束了CMD已退出的旧卡住runner，没有再次结束恢复后的OV。
