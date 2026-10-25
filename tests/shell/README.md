# Godot sidecar 检查

在仓库根目录运行离线检查：

```powershell
node --test tests/shell/godot-sidecar.test.mjs
```

离线检查覆盖五种平台/架构导出计划、架构目录隔离、可执行文件缺失、C# 数据缺失和三平台 HDR 资源位置。
临时文件只写入 `tests/output/shell-s3/`，检查结束时清理。

启动冒烟需要 Windows 桌面会话、可用 GPU、已安装的 `shell/` 依赖，以及本次实际构建的完整目录包。
探针使用现有 Playwright 的 Electron 驱动，无需额外下载浏览器。
不要将探针指向正在运行的生产包。

```powershell
node tests/shell/smoke-godot-sidecar.mjs <目录包的win-unpacked目录> <绝对路径的tests/output/shell-s3/smoke目录>
```

探针使用独立用户目录和随机 server-channel 端口，关闭语音 sidecar 自启。
探针从不同工作目录启动包，验证 Godot ready、手动停止、再次启动和壳正常退出。
随后临时改名测试包的 Godot 可执行文件，验证缺失降级；结束时恢复文件名。
进程检查只读取指定测试目录包的 PID、父 PID 和可执行文件路径，不读取进程命令行。
结果写入 `result.json`，截图写入同目录。
探针不发送聊天、不调用模型、不部署，也不重启生产服务。

无头探针需要 Python 的 `websockets` 包和一份独立导出的 Godot .NET 测试包。
无需 Electron、桌面会话或 GPU。不要指向生产包：

```powershell
python tests/shell/smoke-godot-headless.py --executable <测试包的Godot可执行文件> --output <绝对路径的tests/output/shell-s3/headless目录>
```

也可以用 Godot 编辑器可执行文件并加 `--project <绝对路径的Godot项目目录>`；此时需 .NET 10 SDK 在 PATH 中。
探针从输出目录启动，使用随机端口的临时 WebSocket 主机，验证 ready、正常退出、无运行错误和无 HDR 缺失告警。
结果写入 `result.json`，日志写入 `godot.log`；不发送聊天、不调用模型、不验证图像。

仅校验跨平台导出计划，可从任意工作目录运行：

```powershell
node <仓库绝对路径>/tools/godot-sidecar.mjs --platform darwin --arch arm64 --dry-run
node <仓库绝对路径>/tools/godot-sidecar.mjs --platform linux --arch x64 --dry-run
```

## 同步私有素材到临时工作树

`shell/.gitignore` 忽略了作者模型素材（`stage-ui-spine/src/assets/author-models/`）。新建的临时工作树里没有这些文件，`stage-tamagotchi` 的 typecheck 会在 `spine38/Model.vue` 报两条缺失 JSON 的错误。同样被忽略的还有 `stage-ui` 的 live2d / spine / vrm 模型文件（`albion-spine-38.zip` 缺失时 `electron-vite build` 直接失败）。用下面的脚本从主树复制这两组文件（共 26 个），只补缺失文件，不覆盖已有文件，也不修改源目录：

```powershell
pwsh -NoProfile -File <工作树绝对路径>/tools/sync-shell-private-assets.ps1 -WhatIf
pwsh -NoProfile -File <工作树绝对路径>/tools/sync-shell-private-assets.ps1
```

默认源是 `D:\AI\Wife`，目标是脚本所在的仓库；需要时用 `-SourceRoot` 和 `-TargetRoot` 指定。素材是复制的，不用 junction，删除工作树不会删到源文件。

壳的相关 Vitest 用 Node 25，选择 `scripts/godot-build.test.ts` 和
`src/main/services/airi/godot-stage/launch-options.test.ts`；包含 electron-builder 原生配置校验，不产出平台包。
