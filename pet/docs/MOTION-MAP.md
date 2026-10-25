# 阿尔比恩 · 34 组动作对照表

来源：`albion.zip` → `aerbien_3/aerbien_3.model3.json` 的 `FileReferences.Motions`（共 34 组）。
皮肤：「香气来朱阁」（2024 春节旗袍 L2D，阿尔比恩）。

> 手游里这套皮肤的交互：**立绘可摸（头/身体/特殊部位）、可拖动、有彩蛋**（B 站有专门的彩蛋触发教程）。
> 官方没公开"哪一下播哪个动作"的文档，所以下表的语义是**按组名 + 手游通用习惯推断**的（标了「推断」）。
> 想确认某一组到底长什么样：桌宠窗口控制台执行 `__albionPet.play('组名')`，或看「设置 → 模型 → Live2D」里的动作列表。

## 已经在用的（桌宠自动触发）

| 触发方式 | 动作组 | 说明 |
|---|---|---|
| 空闲（自动轮播） | `idle` `idle1` `idle2` `idle3` `idle4` `idle5` | 6 个变体随机播、1 秒交叉淡化 → 看不出重复 |
| 点她**上 1/3** | `touch_head` | 摸头 |
| 点她**中间** | `touch_body` | 戳身体 |
| 点她**下 1/3** | `touch_special` | 特殊触摸（皮肤彩蛋那一类） |
| 她回复里带 `motion` 标记 | 任意组 | `<\|ACT:{"emotion":"happy","motion":"wedding"}\|>` |

## 全部 34 组

| 组名 | 中文 | 类别 | 备注 |
|---|---|---|---|
| `idle` | 空闲·基本 | 空闲 | 9 秒，原为内部循环 |
| `idle1` ~ `idle5` | 空闲·变体 1~5 | 空闲 | 手游那种"不重样"靠的就是它们 |
| `touch_head` | 摸头 | 触摸 | |
| `touch_body` | 摸身体 | 触摸 | |
| `touch_special` | 特殊触摸 | 触摸 | 彩蛋类（推断） |
| `touch_drag1` ~ `touch_drag4` | 拖动反应 1~4 | 触摸 | 手游里拖动立绘的四个方向/阶段（推断） |
| `touch_idle1` ~ `touch_idle10` | 触摸后空闲 1~10 | 触摸 | 被碰之后的各种小动作/余韵（推断，10 个变体） |
| `login` | 登录 | 事件 | 手游进游戏时的开场动作 |
| `home` | 回港 | 事件 | 回港 welcome |
| `mail` | 邮件 | 事件 | 有邮件提醒 |
| `mission` | 任务 | 事件 | |
| `mission_complete` | 任务完成 | 事件 | |
| `complete` | 完成 | 事件 | 与上者相近（推断） |
| `effect` | 特效 | 事件 | 强调用 |
| `wedding` | 誓约 | 事件 | 婚纱/誓约演出 |
| `main_1` `main_2` `main_3` | 主界面 1~3 | 语音伴随 | 主界面待机语音的配套动作 |

## 后端怎么控制动作

她的回复第一行照旧写标记，需要动作时加 `motion` 字段（`albion-pet.ts` 解析）：

```
<|ACT:{"emotion":"curious","motion":"touch_head"}|>
（歪头）指挥官，怎么了吗？
```

已写进她的人设文件（`profiles/albion/SOUL.md`），她知道自己会做动作。写错组名只会被忽略，不会卡住。

## 模型侧的丝滑改造（想法2）

补丁脚本：`pet/tools/patch_model.py`（可重复运行，幂等；备份在 `_albion_model_backup.zip`）。它做两件事：

1. **加一个大写 `Idle` 动作组**（含 6 个 idle 变体）——
   pixi-live2d-display 把空闲组名写死成 `"Idle"`（`dist/cubism4.js`: `this.groups = { idle: "Idle" }`），
   而模型里只有小写 `idle` → SDK 一直找不到空闲动作。加上后 SDK 会**随机**挑一个播。
2. **给所有 34 个动作写 FadeInTime / FadeOutTime**，原来是 `0.0`（硬切！）；
   空闲类 1.0s、触摸/事件类 0.4/0.5s；并把空闲动作的 `Loop` 关掉，
   让 SDK 用**交叉淡化**衔接，而不是在动作内部硬跳回开头。
