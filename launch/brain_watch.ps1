# 阿尔比恩脑看门狗：WSL 整机停了（待机/断电后）没人拉起脑，桌面和聊天就断很久，这里每 2 分钟补一次。
# 先看 airi.exe：用户用「一键关闭.bat」关掉全部后不该再把脑偷偷拉起来，只有壳在跑才管脑。
# 2026-11-02 起平台桥（127.0.0.1:8660，TG/Discord）也归它看：桥单独死了也跑一次幂等的 start.sh。
$ErrorActionPreference = 'Continue'

if (-not (Get-Process airi -ErrorAction SilentlyContinue)) { exit 0 }

function Test-Brain {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8650/health' -UseBasicParsing -TimeoutSec 5
        return ($r.Content -like '*"brain": "pi"*')
    } catch { return $false }
}

function Test-Bridge {
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8660/health' -UseBasicParsing -TimeoutSec 5
        return ($r.Content -like '*"bridge": "chat"*')
    } catch { return $false }
}

$brainOk = Test-Brain
$bridgeOk = Test-Bridge
if ($brainOk -and $bridgeOk) { exit 0 }

# 只走幂等的 start.sh；绝不做 wsl --shutdown，也不调 launch/start_brain.ps1（那条路会重启 WSL）。
# start.sh 会顺带把平台桥拉起来（没配 token 时桥自己写一行日志说明）。
$out = @(wsl -d Ubuntu -- bash /root/albion-pi/start.sh 2>&1)
$brainOk = Test-Brain
$bridgeOk = Test-Bridge
$ok = $brainOk -and $bridgeOk
$last = ($out | ForEach-Object { "$_".Trim() } | Where-Object { $_ } | Select-Object -Last 1)
if (-not $last) { $last = '(no output)' }

$logDir = Join-Path $PSScriptRoot '..\logs'
New-Item -ItemType Directory -Path $logDir -Force | Out-Null
$result = 'fail'; if ($ok) { $result = 'ok' }
$detail = 'brain={0} bridge={1} {2}' -f $brainOk, $bridgeOk, $last
Add-Content -Path (Join-Path $logDir 'brain-watch.log') -Encoding UTF8 `
    -Value ('{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $result, $detail)

if ($ok) { exit 0 } else { exit 1 }