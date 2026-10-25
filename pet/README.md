# Wife / pet — 阿尔比恩的耳朵与眼睛

目录规范与路线图以根 `docs/FILE_LAYOUT.md`、`docs/PROJECT_MAP.md` 为准（旧的 `STRUCTURE.md`、`ROADMAP.md` 已归档到 `docs/history/`），这里只标「哪个是源码」：

| 路径 | 是什么 |
|---|---|
| `whisper_serve.py` | 耳朵：OpenAI 兼容的 `POST /v1/audio/transcriptions`（faster-whisper），监听 127.0.0.1:9882。用 GSV runtime 的 python 跑，见文件头注释 |
| `tools/eyes/` | 屏幕采集端（capture_agent.py + 启动/自启 bat） |
| `tests/` | 测试与测试数据 |
| `models/`、`logs/`、`archive/` | 模型、采集与 A/B 截图日志、备份 —— **不进库**（见 `.gitignore`） |

## 凭据

`tools/eyes/token.txt` 是采集端本机令牌：**留在磁盘上、不进库**。改采集端令牌时记得两边别搞混。
根启动器和 `tools/eyes/` 下的启动脚本都只从它读（照同目录空的 `token.txt.example` 建，只写一行）；缺了就不起眼睛并报错，没有写死的备用值。详见 `tools/eyes/README.md`。
