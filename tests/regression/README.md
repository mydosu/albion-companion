# 批次回归入口（K6）

每批改动合入后跑同一套离线检查，记录基线 HEAD、命令、通过/失败/跳过和日志位置。
本目录只有编排，不复制任何测试实现；被测代码、依赖和断言都留在各自包里。

```powershell
# 列套件
python tests/regression/run_batch.py --list

# 跑全部（TTS 套件要用装了依赖的解释器）
python tests/regression/run_batch.py --all --python .\tts\irodori\.venv\Scripts\python.exe

# 只跑一项；--out 可固定批次目录，默认 tests/output/regression/<时间戳>/
python tests/regression/run_batch.py --suite tts-unit
```

输出只写 `tests/output/regression/<批次>/`（整目录被 `.gitignore` 忽略）：
`summary.json` 记 `head`、`worktree_dirty`、`python` 和每个套件的 `status/exit/seconds/counts/log`，
每条命令的完整 stdout+stderr 落在同目录 `<套件名>.log`。

退出码：`0` 全部 pass（`skipped` 不算失败）；`1` 有套件 fail 或 timeout；`2` `--out` 非法
（仓库外、`..` 逃逸、直接写 `tests/output` 本身）或目录建不出来——这类参数错误在
启动任何套件之前就返回，只打印一行 `error: …`，不会留下半份日志。
`--out` 必须落在 `tests/output/` 之内，这是仓库“生成结果只写 `tests/output/`”的约束。

## 套件与原生命令对照

| 套件 | 原生命令 | 判据 |
|---|---|---|
| `repo-layout` | `python tools/check_repository.py` | `layout_errors == 0` |
| `tts-unit` | `python -B -m unittest discover -s tests/tts -p 'test_*.py'` | 0 失败（允许 skip） |
| `tts-translate-rules` | `python -I tests/tts/test_translate_rules.py` | 6 个 TestCase 全过 |
| `pet-bat` | `python pet/tools/check_bat.py` | 根目录 .bat 全为 GBK + CRLF |
| `pi-brain-guard` | `node integrations/pi-brain/bin/test_guard_ext.ts` | 输出 `SELFTEST OK` |
| `pi-brain-shell-tools` | `python -B -m unittest discover -s tests/pi-brain -p test_shell_tools.py` | 0 失败（本机回环 HTTP；Windows 需 `pip install tzdata`） |
| `pi-brain-shell-tools-ext` | `node tests/pi-brain/check_shell_tools_ext.ts` | 输出 `SELFTEST OK` |
| `shell-vitest` | Node 25 `vitest.mjs run --maxWorkers=2` | Vitest 返回 0；缺 Node 25 或安装依赖时 `skipped` |

`needs` 里的路径不存在时该套件记为 `skipped` 并写明缺什么，不计失败。

## 自测

```powershell
# 覆盖 --out 边界、超时终止与继续、Node 25 探测；不跑完整套件
python -I tests/regression/test_run_batch.py
```

`test_run_batch.py` 不是套件，不会被 `SUITES` 收录，避免自递归。

## 环境

TTS 两个套件要装好依赖的解释器（`--python .\tts\irodori\.venv\Scripts\python.exe`）；
壳 Vitest 需要在 `shell/` 安装 workspace 依赖，命令使用 Node 25。可显式传入
`--node C:\path\to\node.exe`，或设置 `ALBION_NODE25_PATH`；否则只接受 PATH 中
`node --version` 为 `v25.x` 的解释器。找不到时写明跳过原因，不把 Node 22 当作兼容版本。
其他套件均无需 Node 25。pi-brain 的 `wake.py` / `server.py --selftest` 才要求 `tzdata`，
见 `tests/README.md`。

## 不纳入本入口的

- 真实服务、模型、GPU：`tests/tts/eval/`、`tests/tts/probes/`、`tests/pet/check_ear.py`、`pet/check_all.py`
  （要 :9882/:9889/:8650 或真实推理），改动后单独按 `tests/README.md` 跑。
- 桌面/CDP 与 Electron 页面：`tests/pet/*.mjs`、`:9335` 调试端口相关探针。
- 任何会启动/重启服务的命令。

## 加套件

在 `run_batch.py` 的 `SUITES` 里加一项：`name`、`what`、`cmd`（接收解释器路径的函数）、
`needs`（缺了就跳过）、`parse`（把输出解析成 `{"failed": N, ...}`）。命令必须离线、幂等、
只读仓库，产物只能写 `tests/output/`。
