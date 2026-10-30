# Godot sidecar 构建与运行

Godot 舞台随 Electron 包一起分发。语音模型仍使用 `docs/打包与语音服务.md` 中的外置目录。

在 `shell/` 运行 `pnpm -F @proj-airi/stage-tamagotchi build:unpack`。
`build:win`、`build:mac`、`build:linux` 和直接调用 electron-builder 都会执行同一个构建钩子。
钩子先导入 Godot 资源，再按目标平台导出 release sidecar。
导出失败或 C# 数据不完整时，构建失败。复制资源后再次检查产物，避免缺少 sidecar 的包通过构建。
已有 HDR 天空纹理也复制到 sidecar 目录内，保留既有资源解析器使用的相对路径。
运行包不需要访问源码工作区中的纹理文件。

构建前安装 .NET 10 SDK、Godot 4.7.1 .NET 版和相同版本的 .NET 导出模板。
将 `GODOT4` 设为 Godot 可执行文件的绝对路径，或把 `godot` 加入 PATH。
Godot 自己再去找 .NET：`dotnet` 必须在 PATH 上，或同时设 `DOTNET_ROOT` 指向 .NET 安装目录。
两个都没给时 Godot 会以 `Could not create child process: dotnet --list-sdks` 报错并崩溃，构建在 beforePack 阶段失败；只设 `GODOT4` 不够。
工具路径、下载内容和生成的运行时不入 Git。
Windows 当前仅有 x64 导出预设，构建 Windows ARM64 会明确失败。
macOS 使用 universal 预设，Linux 按目标架构选择 x64 或 arm64。

| 阶段 | 位置 |
|---|---|
| 导出结果 | `shell/engines/stage-tamagotchi-godot/out/{win,mac,linux}/` |
| Windows 包 | `resources/godot-stage/godot-stage.exe` 及完整 C# 数据目录 |
| macOS 包 | `Contents/Resources/godot-stage/godot-stage.app/` |
| Linux 包 | `resources/godot-stage/godot-stage` 及完整 C# 数据目录 |

运行时从 Electron 的 `process.resourcesPath` 查找 sidecar，与启动器的工作目录无关。
导出的进程以可执行文件所在目录为工作目录。
生产包不依赖 `GODOT4`、源码目录或本机 .NET SDK。
模型和视图数据写入用户数据目录下的 `godot-stage/`。

Godot 舞台由设置页按需启动，收到 `stage.ready` 后才切换渲染器。
缺少可执行文件时，主进程返回错误，不创建 WebSocket 服务或子进程。
设置页显示恢复完整安装包的提示，原有内置渲染器继续可用。
已有渲染同步逻辑在舞台停止或报错时恢复内置渲染器。
正常退出壳时，主进程发送 `host.shutdown`，等待两秒；超时后终止本次创建的 sidecar。

离线检查：`node --test tests/shell/godot-sidecar.test.mjs`。
启动冒烟需要 Windows 桌面、可用 GPU 与实际导出的完整包。
构建通过不能替代启动、退出和进程残留检查。
本轮报告原保存在主项目 `tests/output/shell-s3/godot-sidecar.md`；该输出目录已于 2026-10-24 清理，
需要报告时重跑 `node --test tests/shell/godot-sidecar.test.mjs` 生成。
