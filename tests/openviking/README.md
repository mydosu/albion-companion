# OpenViking 守护回归

隔离回归只写主树 `tests/output/ov-leftovers/isolated/` 下的临时目录，仅启动测试自己的短命 Python/CMD 进程，不访问服务、通知会话或私密 profile。测试结束移除自己的临时文件。Windows Job 超时用例只结束测试创建的父子进程，不是生产故障注入。

前置：Windows PowerShell 5.1，Python 3（生产使用 `D:/AI/openviking/venv/Scripts/python.exe`），其环境有 pywin32。非 Windows 会跳过三个 Windows 专用用例，不能把跳过算通过。测试路径从 `__file__` 定位，不依赖 cwd。

```powershell
& D:/AI/openviking/venv/Scripts/python.exe -m unittest discover -s tests/openviking -p test_guard.py -v
powershell.exe -NoProfile -File tests/openviking/test_observer.ps1
# 以下是有副作用的在线验收，需要明确授权后执行：
& D:/AI/openviking/venv/Scripts/python.exe tests/openviking/verify_live.py --health --doctor --accept --output D:/AI/Wife/tests/output/ov-leftovers/live-verification.json
```

覆盖：采样不启动 Hermes、共享请求预算、认证错误分类、恢复取消、非零退出/超时节流、同批提取失败去重、新批次、发送中恢复/新故障、过期事件、维护态、固定三个事件槽、旧节流迁移、坏状态拒绝发送、采样窗口、禁工具单轮 CLI、通知消费者并发、主守护锁、跨进程锁释放、退出码及 Windows Job 子进程回收、实际 CMD 隔离运行与 8082 停用。观察器检查 PowerShell 语法、正常存活、PID 身份变化拒绝、正常退出的返回码保留。

生产验证另行在授权部署后执行：Windows/WSL GET `/health` 与 `/ready`、插件 doctor（必须先在内存剔除凭据字段再保存）、注册 MCP find/read、`bash /root/albion-pi/accept.sh`。这些需要当前在线服务/WSL/模型；accept 会写专用验收会话，属于有副作用的在线验收，不混入离线单测。产物写主树 `D:/AI/Wife/tests/output/ov-leftovers/`，不得留在可删除临时 worktree。

退出告警回归另覆盖：服务恢复后仍通知、成功后同批去重、5分钟过期、并发追加半行/坏事件忽略、轮转去重、不可达与退出分槽及失败节流。生产故障注入不是离线测试的一部分；2026-10-21实测证据与上游余额导致的真实投递失败见主树`tests/output/ov-s5/REPORT.md`。

Windows管道继承回归：模拟测试CMD创建一个持续存活且继承stdout的测试子进程，验证runner不等子进程退出就能释放本轮；清理仅终止并等待本测试创建的子进程。
