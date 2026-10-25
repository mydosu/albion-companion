# 启动沙箱里她的脑（Pi 版）—— 幂等，以端口为准。
# 三个根启动器（一键启动.bat、一键启动-dev.bat、start_all.bat）第 4 步都调它。
# 以前的 Hermes 版启动器 _启动脑WSL.ps1 已于 2026-10-16 删除，要回看见 git 历史（5f755e83 及以前）。
# 跨边界自检：WSL mirrored 模式偶发开机后镜像网卡没挂上，脑在 WSL 里活着但 Windows 摸不到 8650
# → 三次不通就 wsl --shutdown 重进一次；仍不行交人工。
$ErrorActionPreference = 'Continue'

function Test-PiBrainFromWin {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8650/health' -UseBasicParsing -TimeoutSec 5
        return (($r.Content -like '*"status"*') -and ($r.Content -like '*"brain": "pi"*'))
    } catch { return $false }
}

for ($i = 1; $i -le 3; $i++) {
    # WSL 里的 Hermes 已于 2026-10-07 删除；她的脑现在只靠 /root/albion-pi 活着。
    wsl -d Ubuntu -- bash -lc "bash /root/albion-pi/start.sh" | Out-Null
    if (Test-PiBrainFromWin) { Write-Output "brain-up (pi)"; exit 0 }
    wsl --shutdown
    Start-Sleep -Seconds 4
}
Write-Output "brain-fail"
exit 1
