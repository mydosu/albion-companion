# 阿尔比恩的眼睛 · 采集端

**装在你日常主用的那台电脑上**（Tailscale 名 `albion`，<主力机 Tailscale IP>）。

只做一件事：被问到时截一张屏。不主动外发、不常驻后台、不碰别的东西。
只用标准库 + Windows 自带 .NET 截图，**不需要 pip 装任何包**。

## 跑起来

**本机（阿尔比恩所在的这台）**：不用手动开 —— `D:\AI\Wife\一键启动.bat`（以及 `start_all.bat`、`一键启动-dev.bat`、`start_eyes.bat`）会以 `--bind 127.0.0.1 --token-file <本目录 token.txt>` 自动拉起（端口 8790），壳里「自己这台」那个来源就是它。
token 由采集端自己读文件第一行非空内容，**不出现在进程命令行里**；文件缺失或为空时采集端直接退出，不会换成随机值。

**指挥官那台**：双击 `启动采集端.bat`，它同样用 `--token-file` 从同目录 `token.txt` 读 token。
（也认环境变量 `ALBION_EYES_TOKEN`；`--token <值>` 仍可用但值会进命令行。三者都不给时会随机生成一个，只适合临时试。）

### token.txt（每台一份，不入库）

- 位置：本目录 `pet\tools\eyes\token.txt`，照同目录的 `token.txt.example`（空文件）建；
  里面**只写一行 token**，不加注释、不加引号。`.gitignore` 已经忽略它。
- 缺这个文件或文件是空的：启动器不起眼睛、明确报错（根入口统一由 `start_all.bat` 写进 `logs\eyes.err.log`），
  其余服务照常起；不会退回任何写死的值。
- 同一个值还要填到用它的地方：WSL 脑的 `agent/.env`（`ALBION_EYES_MINE_TOKEN` / `ALBION_EYES_HIS_TOKEN`）。
  2026-10-16 起壳里不再直接取图（原来的 `albion-eyes.ts` 和 `settings/albion/eyes-*` 设置已撤），看东西只走脑里的 `look` 工具。

## 只想验证一下

```
python capture_agent.py --selftest        # 截一张就退出，打印分辨率与体积
curl "http://<主力机 Tailscale IP>:8790/health"  # 看它活着没
curl -H "Authorization: Bearer <TOKEN>" -o shot.jpg "http://<主力机 Tailscale IP>:8790/shot"
```

## 安全

- 默认**只绑 Tailscale 地址**（不是 0.0.0.0），公网/局域网其他网段都连不上；
- `--lan`（`启动采集端.bat` 已带）再多绑本机的局域网地址（只认 10/8、192.168/16），同一局域网直连、
  不走 Tailscale 中继——照 MWB 的做法（2026-10-15：taihou 开着 PrivadoVPN 时 Tailscale 只能走香港中继，
  一张整屏 35–60 秒）。首次启动 Windows 防火墙会问，选「专用网络」允许；
- 取图前会把敏感窗口涂黑：标题/进程名命中 `$REDACT_WORDS` 的整窗、以及系统报出的所有密码框；
- `/shot` 与 `/cam` 必须在 `Authorization: Bearer …` 请求头中带 token；不要把 token 放在 URL 查询串里；
- 如果窗口/密码框遮挡检查失败、超时或截图输出不完整，`/shot` 会拒绝返回图片；HTTP 错误和访问日志不包含 token 或查询串；
- 想关就关窗口。

## 摄像头

同一个进程还管摄像头（`/cam`）：用 ffmpeg 抓一帧，先转正（这台摄像头侧装，默认顺时针转 90°，`--cam-rotate 0/90/180/270` 可改），再按 `--max-width` 只缩不放。脑里 `look` 的 cam 那一路取的就是它，只交本地视觉模型转成文字。
（壳里原来的「在场检查」已于 2026-10-16 随壳端主动定时器一起撤掉。）

## 开机自启（可选）

`安装开机自启.bat` / `取消开机自启.bat`：往启动文件夹放一个无窗口后台脚本。
**本机那台不需要装**（一键启动会拉）；装了就和新方式重复，建议取消。
