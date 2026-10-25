# 包级复核模板（K7）

每个变更包做完后单独复核一次，一个包一份结论；不要用一份总回归结果代替包级审查。
复核人只读：不改被复核的代码，不部署、不重启、不推送。报告写到
`tests/output/<批次>/review-<包名>.md`（该目录不入库），命令与原始输出同目录留档。

## 复核对象

- 包名：<shell / pi-brain / pet / tts / docs / tests …>
- 基线 commit：<base>　当前 HEAD：<head>　范围：`<base>..<head>`
- 本包改动：`git diff --name-status <base>..<head> -- <本包路径>`

## 1. 范围核对

| 检查 | 命令 | 结果 |
|---|---|---|
| 改动路径都在任务允许范围内 | `git diff --name-status <base>..<head>` 与任务清单逐项对照 | |
| 没有夹带其它包的文件 | 同上 | |
| 空白/冲突标记 | `git diff --check <base>..<head>` | |
| 二进制或大文件 | `git diff --stat <base>..<head>` | |

## 2. 契约与引用

- 改到的导出/函数/路径，谁在用：`git grep -n '<符号或路径>'`
- 被移动/删除的路径，引用是否同步（文档、启动器、import、fixture、输出目录）
- 相对链接、`__file__` 定位、cwd 前置条件是否仍然成立

## 3. 边界与失败行为

- 缺依赖 / 缺文件 / 空输入 / 超时时的行为，是否与改动前一致或更安全
- 是否会启动、重启或写入生产文件（应为否）

## 4. 对应测试

| 测试 | 命令 | 基线与本包对比 |
|---|---|---|
| 包内单测 | | 通过数/失败数是否变化 |
| 批次回归 | `python tests/regression/run_batch.py --suite <套件>` | |

只报实际跑过的；跳过要写原因。

## 5. 敏感材料

- `git diff <base>..<head>` 里是否有凭据、token、私钥、个人数据
- Gitleaks：`gitleaks git . --log-opts=<base>..<head> --redact`（只在准备入库时跑）

## 结论

- 通过 / 通过（有遗留）/ 不通过
- 未验证项（真实服务、模型、GPU、Electron 页面、生产部署）逐条列出
- 遗留问题与建议的后续动作