<#
.SYNOPSIS
    Hermes 网关看门狗：网关进程不存在才拉起，绝不产生第二个实例。

.DESCRIPTION
    由 Windows 计划任务每 5 分钟调用一次（经 tools\hermes_watchdog.vbs 以隐藏窗口启动，无控制台弹窗）。

    判定逻辑：
      1. 枚举所有 hermes 网关进程（`hermes_cli.main gateway run` / `hermes.exe gateway run`）。
         找到一个就什么都不做 —— 这是防双实例的第一道闸。
      2. 一个都没有 → 读 %LOCALAPPDATA%\hermes\gateway.pid 交叉核对（pid 文件里的进程若还活着也算在跑）。
      3. 确实没有 → 用和登录项 Hermes_Gateway.vbs 完全相同的方式拉起
         （wscript.exe + gateway-service\Hermes_Gateway.vbs，内部 sh.Run(...,0,False) 隐藏无窗口），
         并且经 Win32_Process.Create 启动，彻底脱离计划任务/调用者的进程树
         （2026-11-03 那次掉线就是网关被别的会话进程树连带杀掉，见 logs/hermes-watchdog.log 头部的背景说明）。
      4. 拉起后再确认一次：等最多 90 秒观察是否出现新网关进程；期间若发现已有实例则立即收手，不重复拉起。

    明确不做的事：
      - 不调用 `hermes gateway status`（该命令会持续挂住，计划任务里会卡死）。
      - 不写/不改 gateway.pid、gateway_state.json 等网关自有文件。
      - 不使用 start /min、cmd、bat。
      - 不改人设/配置/模型。

    日志：logs\hermes-watchdog.log（默认 D:\AI\Wife\logs；只在“有动作”或“有异常”时追加，健康运行时只刷新状态文件，不刷屏）
    状态：logs\hermes-watchdog-state.json（last_check / last_state / last_action / last_launch_at / gateway_pid）
    锁：Local\HermesGatewayWatchdog 命名互斥，防止两次触发叠加。

.PARAMETER HermesHome
    Hermes 安装目录，默认 %LOCALAPPDATA%\hermes。

.PARAMETER LogFile
    看门狗日志路径。

.PARAMETER StateFile
    看门狗状态文件路径。

.PARAMETER LaunchCooldownSeconds
    两次拉起尝试之间的最小间隔（默认 180 秒），避免网关起不来时每 5 分钟疯狂重启。

.PARAMETER SimulateNoGateway
    演练模式：假装网关不在，走完整决策流程并打印将要执行的命令，但**不真的拉起**。用于验证“该动的时候会动”。

.PARAMETER NoMutex
    跳过互斥锁（仅手工排障用）。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\Wife\tools\hermes_watchdog.ps1

.EXAMPLE
    # 演练：不杀网关，验证“没有网关时会怎么走”
    powershell -NoProfile -ExecutionPolicy Bypass -File D:\AI\Wife\tools\hermes_watchdog.ps1 -SimulateNoGateway

.NOTES
    计划任务注册（当前用户、登录时运行、每 5 分钟、无窗口）——本文件落地时已注册为 “Hermes Gateway Watchdog”：

        $act = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\wscript.exe" `
                    -Argument '"D:\AI\Wife\tools\hermes_watchdog.vbs"'
        $trg = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
                    -RepetitionInterval (New-TimeSpan -Minutes 5)
        $set = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew `
                    -ExecutionTimeLimit (New-TimeSpan -Minutes 10) `
                    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
        $prn = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                    -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName 'Hermes Gateway Watchdog' -Action $act -Trigger $trg `
                    -Settings $set -Principal $prn `
                    -Description 'Hermes 网关看门狗：每 5 分钟检查，进程没了才用登录项同款方式拉起（无窗口、不产生双实例）'

    查看/手动触发：
        Get-ScheduledTask -TaskName 'Hermes Gateway Watchdog' | Get-ScheduledTaskInfo
        Start-ScheduledTask -TaskName 'Hermes Gateway Watchdog'

    暂停/停用（例如你想让网关先停一会儿）：
        建空文件 %LOCALAPPDATA%\hermes\.watchdog-paused  → 看门狗直接跳过，不拉起、不写日志
        删掉该文件即恢复守护
        彻底停用：Unregister-ScheduledTask -TaskName 'Hermes Gateway Watchdog' -Confirm:$false
#>

[CmdletBinding()]
param(
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA 'hermes'),
    [string]$LogFile = 'D:\AI\Wife\logs\hermes-watchdog.log',
    [string]$StateFile = 'D:\AI\Wife\logs\hermes-watchdog-state.json',
    [int]$LaunchCooldownSeconds = 180,
    [switch]$SimulateNoGateway,
    [switch]$NoMutex
)

$ErrorActionPreference = 'Stop'
$MaxLogBytes = 256KB
$MutexName = 'Local\HermesGatewayWatchdog'

$script:Tag = 'hermes-watchdog'

function Write-WLog {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'ERROR')][string]$Level = 'INFO')

    $line = '{0} [{1}] [pid {2}] {3}' -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'), $Level, $PID, $Message
    try {
        $dir = Split-Path -Parent $LogFile
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        Add-Content -LiteralPath $LogFile -Value $line -Encoding utf8
        $item = Get-Item -LiteralPath $LogFile -ErrorAction SilentlyContinue
        if ($item -and $item.Length -gt $MaxLogBytes) {
            $kept = Get-Content -LiteralPath $LogFile -Tail 1500
            Set-Content -LiteralPath $LogFile -Value $kept -Encoding utf8
        }
    } catch {
        Write-Output $line
    }
    if ($Level -ne 'INFO') { Write-Output $line } else { Write-Verbose $line }
}

function Save-State {
    param([hashtable]$State)

    $State['last_check'] = (Get-Date).ToString('o')
    try {
        $dir = Split-Path -Parent $StateFile
        if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
        ($State | ConvertTo-Json -Depth 4) | Set-Content -LiteralPath $StateFile -Encoding utf8
    } catch {
        Write-WLog "状态文件写入失败：$($_.Exception.Message)" 'WARN'
    }
}

function Read-State {
    $blank = @{ last_state = $null; last_action = $null; last_launch_at = $null; gateway_pid = $null }
    if (-not (Test-Path -LiteralPath $StateFile)) { return $blank }
    try {
        $raw = Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json
        $out = @{}
        foreach ($k in 'last_state', 'last_action', 'last_launch_at', 'gateway_pid') {
            $out[$k] = $raw.$k
        }
        return $out
    } catch {
        return $blank
    }
}

# 网关进程 = 命令行同时含 hermes 与 "gateway run"（兼容 python -m hermes_cli.main / hermes.exe）
function Get-GatewayProcess {
    $candidates = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe' OR Name='hermes.exe'" -ErrorAction SilentlyContinue
    foreach ($p in $candidates) {
        $cmd = $p.CommandLine
        if (-not $cmd) { continue }
        if ($cmd -match 'gateway\s+run' -and $cmd -match 'hermes') {
            [pscustomobject]@{
                ProcessId    = $p.ProcessId
                Name         = $p.Name
                CreationDate = $p.CreationDate
                CommandLine  = $cmd
            }
        }
    }
}

function Get-PidFileInfo {
    $pidFile = Join-Path $HermesHome 'gateway.pid'
    if (-not (Test-Path -LiteralPath $pidFile)) { return $null }
    try {
        return (Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json)
    } catch {
        return $null
    }
}

function Get-LauncherPath {
    $vbs = Join-Path $HermesHome 'gateway-service\Hermes_Gateway.vbs'
    if (-not (Test-Path -LiteralPath $vbs)) { return $null }
    return $vbs
}

$mutex = $null
try {
    if (-not $NoMutex) {
        $mutex = New-Object System.Threading.Mutex($false, $MutexName)
        if (-not $mutex.WaitOne(0)) {
            Write-Verbose 'another watchdog run is in progress; exiting'
            return
        }
    }

    # 暂停开关：用户想主动让网关停着时，建 <HermesHome>\.watchdog-paused 即可，看门狗不再拉起
    $pauseFile = Join-Path $HermesHome '.watchdog-paused'
    if (Test-Path -LiteralPath $pauseFile) {
        Write-Verbose "paused via $pauseFile"
        return
    }

    $state = Read-State
    $procs = @(Get-GatewayProcess)
    $pidInfo = Get-PidFileInfo

    $pidFileAlive = $false
    if ($pidInfo -and $pidInfo.pid) {
        $alive = Get-Process -Id ([int]$pidInfo.pid) -ErrorAction SilentlyContinue
        if ($alive) { $pidFileAlive = $true }
    }

    if ($SimulateNoGateway) {
        Write-WLog '演练模式（-SimulateNoGateway）：忽略现有网关，按“无网关”分支继续，不会真的拉起。' 'WARN'
        $procs = @()
        $pidFileAlive = $false
    }

    # --- 分支 1：已有网关在跑 ---
    if ($procs.Count -gt 0) {
        $ids = ($procs | ForEach-Object { $_.ProcessId }) -join ','
        $matchesPidFile = $pidInfo -and ($procs.ProcessId -contains [int]$pidInfo.pid)
        if (-not $matchesPidFile) {
            Write-WLog ("网关在跑（PID {0}），但 gateway.pid 记录不一致（文件 {1}）；不动手，避免双实例。" -f `
                    $ids, $(if ($pidInfo) { $pidInfo.pid } else { '缺失' })) 'WARN'
        } else {
            Write-Verbose "gateway healthy (PID $ids)"
        }
        if ($state['last_state'] -ne 'ok' -or $state['gateway_pid'] -ne $ids) {
            Write-WLog "网关正常，无需动作（PID $ids）。"
        }
        Save-State @{ last_state = 'ok'; last_action = 'none'; last_launch_at = $state['last_launch_at']; gateway_pid = $ids }
        return
    }

    # --- 分支 2：pid 文件里的进程还活着（但命令行没匹配上，稳妥起见当它在跑）---
    if ($pidFileAlive) {
        Write-WLog ("gateway.pid 记录的 PID {0} 存在，但未能确认是 hermes gateway；不动手，避免双实例。" -f $pidInfo.pid) 'WARN'
        Save-State @{ last_state = 'unknown'; last_action = 'none'; last_launch_at = $state['last_launch_at']; gateway_pid = $pidInfo.pid }
        return
    }

    # --- 分支 3：真的没有网关，准备拉起 ---
    $launcher = Get-LauncherPath
    if (-not $launcher) {
        Write-WLog ("找不到登录项同款启动器，放弃拉起：{0}" -f (Join-Path $HermesHome 'gateway-service\Hermes_Gateway.vbs')) 'ERROR'
        Save-State @{ last_state = 'missing-launcher'; last_action = 'none'; last_launch_at = $state['last_launch_at']; gateway_pid = $null }
        exit 1
    }

    $wscript = Join-Path $env:SystemRoot 'System32\wscript.exe'
    $cmdLine = '"{0}" "{1}"' -f $wscript, $launcher

    if ($SimulateNoGateway) {
        Write-WLog ("演练：网关不存在，将执行 Win32_Process.Create → {0}" -f $cmdLine) 'WARN'
        Save-State @{ last_state = 'simulated-down'; last_action = 'simulated-launch'; last_launch_at = $state['last_launch_at']; gateway_pid = $null }
        return
    }

    if ($state['last_launch_at']) {
        try {
            $lastLaunch = [datetime]::Parse($state['last_launch_at'], $null, [System.Globalization.DateTimeStyles]::RoundtripKind)
            $elapsed = ((Get-Date).ToUniversalTime() - $lastLaunch.ToUniversalTime()).TotalSeconds
            if ($elapsed -lt $LaunchCooldownSeconds) {
                Write-WLog ("{0:N0} 秒前刚拉起过，仍在冷却期（{1} 秒）；本轮跳过。" -f $elapsed, $LaunchCooldownSeconds) 'WARN'
                Save-State @{ last_state = 'down-cooldown'; last_action = 'skipped'; last_launch_at = $state['last_launch_at']; gateway_pid = $null }
                return
            }
        } catch { }
    }

    Write-WLog "网关不在（无 gateway 进程），开始拉起：$cmdLine" 'WARN'
    $launchAt = (Get-Date).ToString('o')
    try {
        $res = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmdLine }
    } catch {
        Write-WLog ("Win32_Process.Create 调用失败：{0}" -f $_.Exception.Message) 'ERROR'
        Save-State @{ last_state = 'down'; last_action = 'launch-failed'; last_launch_at = $launchAt; gateway_pid = $null }
        exit 1
    }

    if ($res.ReturnValue -ne 0) {
        Write-WLog ("拉起失败，Win32_Process.Create ReturnValue={0}" -f $res.ReturnValue) 'ERROR'
        Save-State @{ last_state = 'down'; last_action = 'launch-failed'; last_launch_at = $launchAt; gateway_pid = $null }
        exit 1
    }
    Write-WLog ("启动器已派发（wscript PID {0}），等待网关进程出现……" -f $res.ProcessId)

    $deadline = (Get-Date).AddSeconds(90)
    $newPid = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 5
        $now = @(Get-GatewayProcess)
        if ($now.Count -gt 1) {
            Write-WLog ("发现 {0} 个网关进程（PID {1}）——异常，可能是并发拉起，请人工核对。" -f `
                    $now.Count, (($now | ForEach-Object { $_.ProcessId }) -join ',')) 'ERROR'
            break
        }
        if ($now.Count -eq 1) { $newPid = $now[0].ProcessId; break }
    }

    if ($newPid) {
        Write-WLog ("拉起成功，网关 PID {0}。" -f $newPid)
        Save-State @{ last_state = 'ok'; last_action = 'launched'; last_launch_at = $launchAt; gateway_pid = "$newPid" }
    } else {
        Write-WLog '拉起后 90 秒内没看到网关进程，请查 hermes logs\gateway.log 与 gateway-stdio.log。' 'ERROR'
        Save-State @{ last_state = 'down'; last_action = 'launched-unconfirmed'; last_launch_at = $launchAt; gateway_pid = $null }
        exit 1
    }
} finally {
    if ($mutex) {
        try { $mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
}
