> ### 阿尔比恩桌宠 · v1.0.0
>
> 本仓库是 [Project AIRI](https://github.com/moeru-ai/airi) 的 fork，用于把 **阿尔比恩** 这个 Live2D 模型做成可以日常挂着的桌面宠物：模型包里作者写的交互数据（`Motions` / `HitAreas` / `ParamHit` / `Controllers`）照原样实现，动作链、优先级、循环都按作者给的值走；语音与会话全部跑在本地，不依赖云。
>
> #### 实现要点
> - **动作用作者数据驱动**：`FileLoop` / `NextMtn` / `Priority` / `Interruptable` 直接读模型包字段，不自建动作名单或循环窗口；空闲的两层（身体 + 特效层）同时播放。
> - **按 `Priority` 做打断仲裁**：新动作优先级不低于当前在播动作时才切；更低优先级的动作，只有在当前动作标注了 `Interruptable` 时才能打断。
> - **交互命中走模型坐标系**：用 Cubism 的 `hitTest()` 判断部位，不写死屏幕坐标，因此缩放窗口、改画布尺寸都不会错位。
> - **本地链路**：TTS（Irodori，日语多音色）、STT（paraformer）与对话后端均部署在本机。
>
> #### 关于素材与许可
> - Live2D 模型及其动作数据、语音来自游戏《碧蓝航线》（皮肤「香气来朱阁」）与模型作者，**不随本仓库分发**（`**/assets/live2d/models/*` 已列入 `.gitignore`），仅供本地使用。
> - 运行时依赖 **Live2D Cubism Core**（© Live2D Inc.，适用其专有许可协议），同样不单独分发。
> - 本 fork 自身的修改沿用上游的 MIT 许可；上述第三方素材不适用该许可。


---

## 与上游 Project AIRI 的关系

本目录是 [Project AIRI](https://github.com/moeru-ai/airi)（MIT）的 fork，只保留桌面壳需要的部分：
`apps/stage-tamagotchi`（Electron 壳）、`packages/*`（UI / 音频 / 插件 SDK / provider 等）、
`engines/stage-tamagotchi-godot`（Godot 舞台）、`server/`（壳会用到的 API 类型与 SDK）。

上游的**文档站**（`docs/`，462 MB 的 Vitepress 站点与多语言内容）、**聊天机器人集成**（`integrations/`）、
**computer-use-mcp 服务**（`services/`）、**插件样例**（`plugins/`）、**AI 代理技能库**（`.agents/`）
已于 2026-11-01 从工作树和 pnpm workspace 移除（历史仍在 git 里，可 `git log -- shell/docs` 回看）。
需要它们时看上游仓库，不要在本仓库里重建。

## 本地构建（Windows）

```powershell
$env:GODOT4     = 'D:\AI\_tools\godot-4.7.1\Godot_v4.7.1-stable_mono_win64\Godot_v4.7.1-stable_mono_win64.exe'
$env:DOTNET_ROOT = 'D:\AI\Wife\.local\godot-tools\dotnet'
$env:PATH        = "$env:DOTNET_ROOT;$env:PATH"
cd shell
pnpm install --prefer-offline
pnpm -F @proj-airi/stage-tamagotchi build:unpack
```

产物在 `apps/stage-tamagotchi/dist/win-unpacked/airi.exe`；启动、关闭与语音服务见仓库根的
[`使用说明.md`](../使用说明.md) 和 [`docs/PROJECT_MAP.md`](../docs/PROJECT_MAP.md)。

## 许可

上游 MIT；本 fork 的修改同样 MIT。Live2D 模型、Spine 包、语音参考音等第三方素材不随仓库分发，
运行时依赖 Live2D Cubism Core（专有许可），细节见仓库根 `LICENSE`。