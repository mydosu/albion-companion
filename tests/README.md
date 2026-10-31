# 测试入口

所有命令默认在仓库根 `D:\AI\Wife` 的 PowerShell 执行。可执行测试与生产源码分开；
固定材料保留原值，生成结果统一写 `tests/output/`。

```powershell
# 布局门禁，标准库即可；从其他 cwd 也能执行
python tools/check_repository.py

# CPU / mock 回归，不发起真实翻译或 GPU 推理
& .\tts\irodori\.venv\Scripts\python.exe -B -m unittest discover -s tests/tts -p 'test_*.py'
# 翻译规则断言（已是 unittest.TestCase，可直跑，也会被上面的 discover 收集）
& .\tts\irodori\.venv\Scripts\python.exe -B tests/tts/test_translate_rules.py

# Windows 启动入口隔离接线：夹具替换端口检查/服务启动，不停启生产
& .\tts\irodori\.venv\Scripts\python.exe -B -m unittest discover -s tests/pet -p test_launchers.py

# 真实 STT 验收：需 :9882；从 data/_zh_test.wav 读固定中文音频
& .\tts\GPT-sovits\runtime\python.exe tests/pet/check_ear.py

# 已有服务自检：保持原位，因为启动器调用它；含外部视觉服务检查
& .\tts\GPT-sovits\runtime\python.exe pet/check_all.py 0

# 查看评估参数（不生成语音）
& .\tts\irodori\.venv\Scripts\python.exe tests/tts/eval/ab_stream_v4.py --help
```

批次回归入口（K6）：`python tests/regression/run_batch.py --all --python .\tts\irodori\.venv\Scripts\python.exe`，
只跑离线套件，结果写 `tests/output/regression/<批次>/`，清单与排除项见 `tests/regression/README.md`。
`--out` 只能落在 `tests/output/` 内，越界或建不出来时报错并以 2 退出，见同目录 README。

## Windows 前置条件：tzdata

`integrations/pi-brain/bin/wake.py` 在模块导入时就构造 `ZoneInfo("Asia/Shanghai")`，
`server.py --selftest` 也经 wake 间接依赖它；Windows 版 Python 不自带 IANA 时区库，
缺 `tzdata` 时这两个自检在 import 阶段就失败：

```
zoneinfo._common.ZoneInfoNotFoundError: 'No time zone found with key Asia/Shanghai'
```

装到实际执行自检的解释器里：`python -m pip install tzdata`；
确认：`python -c "import tzdata; print(tzdata.__version__)"`。
`tts\irodori\.venv`（3.10）自带 tzdata 2025.3，可直接跑通这两个自检。
`tests/regression/run_batch.py` 的 5 个套件都不导入 zoneinfo，不需要 tzdata；
TTS 套件仍要求装了 fastapi 等依赖的解释器（见上面的 `--python`）。

## 退出码约定

以现有脚本的实际行为为准，新脚本沿用同一套，不要另起：

| 脚本 | 0 | 非 0 |
|---|---|---|
| `tools/check_repository.py` | 没有布局错误；带 `--gitleaks` 时再返回 gitleaks 自己的退出码 | `1`：布局错误、Git 根不是本仓库，或 Gitleaks 命中 |
| `pet/tools/check_bat.py` | 全部 .bat 为 GBK + CRLF | `1`：有文件不合格 |
| `tests/tts` 的 unittest、`tests/tts/test_translate_rules.py` | 全过（允许 skip） | `1`：有失败/错误 |
| `tests/regression/run_batch.py` | 全部 pass（`skipped` 不算失败） | `1`：有套件 fail/timeout；`2`：`--out` 越界/建不出来（在启动套件之前返回）；超时会终止该套件的子进程树并继续后续套件 |
| `integrations/pi-brain/bin/wake.py`、`server.py --selftest` | 打印 `WAKE SELFTEST OK` / `HOST SELFTEST OK` | 断言失败抛 `AssertionError` → `1`，没有部分成功一说 |
| `integrations/pi-brain/bin/test_guard_ext.ts` | 打印 `GUARD_EXT SELFTEST OK` | 抛错 → `1` |
| `tests/pet/*.mjs` | 结论通过 | `1`：断言不符、连不上 :9335、连接没得出结论就关闭、超时（默认 15s，`ALBION_PROBE_TIMEOUT_MS` 可改）或抛异常；`_verify_calib.mjs` 另用 `3` 表示模块未热更新（要刷新页面后重跑，不算通过） |

`tests/tts/eval/` 真正运行时会请求 :9881/:9889，并可能触发翻译费用与 GPU 推理。
`probes/emotion_bench.py` 同样只打生产 :9889（`/v2/render` 白名单内的锚），结果写
`tests/output/<运行名>/`，不能计入纯单元测试；用法与前置条件见 `tests/tts/README.md`。
`probes/say_albion_mood.py` 请求真实 TTS，结果写入 `tests/output/tts/mood/`，
不能把它计入纯单元测试。

`tests/pet/*.mjs` 是历史桌面/CDP 探针，需要 Electron 调试端口 :9335 以及对应页面；
少量探针还依赖原始角色素材。脚本可以解析不等于对应场景已通过。
2026-10-24 整理后只留三个：`_look_tag_filter_check.mjs`、`_author_engine_check.mjs`、`_verify_calib.mjs`；
其余一次性探针与 `spine-official/` 离线对照工程（含外部 sr38 依赖）已删除，回看用 git 历史。
它们的**退出码可信**，可以直接用 `$LASTEXITCODE` 判定，和输出里的结论一致：
`0` 只有结论通过才给；断言不符、连不上 :9335、连接在得出结论前关闭、超时、抛异常都是 `1`
（2026-10-31 修了 `_verify_calib.mjs` 之前 FAIL 也退 0 的问题，另两个本来就退 1）。
前置条件没满足同样是 `1`——连不上就是失败，不能当通过；只有拿到 `0` 才算这个场景验过。
旧的会修改生产源码的校准脚本（`archive/legacy/pet/tests/`）已删除，见 git 历史。

壳的 Vitest 仍由原 workspace 管理。在 `shell/` 执行：

```powershell
pnpm typecheck
pnpm lint
pnpm exec vitest run --config packages/stage-ui/vitest.config.ts
```

整仓类型检查默认并行，内存不足可使用相同 filters 串行：
`pnpm -r --workspace-concurrency=1 -F='./packages/*' -F='./apps/*' -F='./server/**' -F='./docs' typecheck`。
不要通过修改生产源码“修复”与本次整理无关的既有检查错误。
