# docs/pet — 原 pet 目录的文档（2026-11-02 拆目录后留在这里）

原 pet 目录已经拆成三个功能目录，源码各自带 README：

| 原来在 pet 目录 | 现在在哪 | 看哪份说明 |
|---|---|---|
| `whisper_serve.py`、`start_ear.bat`、`models/` | `ear/` | `ear/README.md`（uv 环境、引擎开关、模型目录） |
| 原 `tools/eyes/`、原视觉机部署包 | `eyes/`、`eyes/vision-server/` | `eyes/README.md`、`eyes/vision-server/README.md` |
| `widget/` | `widget/` | `教程/10-状态挂件.md` |
| `tools/patch_model.py`、`spine_face_eye_*.py` | `tools/spine/` | `docs/pet/MOTION-MAP.md` |
| `tools/check_bat.py` | `tools/check_bat.py` | `tests/regression/README.md` |
| 原 `docs/` 子目录 | `docs/pet/` | 本目录 |
| `tests/` | `tests/ear/`、`tests/eyes/`、`tests/spine/` | `tests/README.md` |

目录规范与路线图以根 `docs/FILE_LAYOUT.md`、`docs/PROJECT_MAP.md` 为准（旧的 `STRUCTURE.md`、`ROADMAP.md` 已归档到 `docs/history/`）。

## 凭据

采集端令牌 `eyes/token.txt`：**留在磁盘上、不进库**。照同目录空的 `token.txt.example` 建，只写一行；
根启动器和 `eyes/` 下的启动脚本都只从它读，缺了就不起眼睛并报错，没有写死的备用值。详见 `eyes/README.md`。
