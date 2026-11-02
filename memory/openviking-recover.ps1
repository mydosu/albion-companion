[CmdletBinding()]
param(
    [ValidateSet('Ensure', 'Force')]
    [string]$Mode = 'Ensure',
    [ValidatePattern('^[A-Za-z0-9_-]{1,40}$')]
    [string]$Caller = 'Unspecified'
)

$ErrorActionPreference = 'Stop'
$ServerRoot = 'D:\AI\openviking'
$ServerExe = Join-Path $ServerRoot 'venv\Scripts\openviking-server.exe'
$ConfigPath = Join-Path $ServerRoot 'ov.conf'
$DataPath = Join-Path $ServerRoot 'data'
$LogRoot = 'D:\AI\scripts\logs\openviking-recovery'
$StatePath = 'D:\AI\scripts\logs\.openviking-recovery-state.json'
$MaintenanceFlag = 'D:\AI\scripts\logs\.openviking-maintenance'
$DataBackupRoot = 'D:\AI\Wife\backup\profile-archives\openviking-recovery'
$MutexName = 'Local\AlbionOpenVikingRecovery'
$script:LogPath = Join-Path $LogRoot 'recovery.log'
$script:RunId = [Guid]::NewGuid().ToString('N').Substring(0, 12)
$script:RunTimer = [System.Diagnostics.Stopwatch]::StartNew()

function Write-RecoveryLog {
    param([string]$Message)
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    if ((Test-Path -LiteralPath $script:LogPath) -and (Get-Item -LiteralPath $script:LogPath).Length -gt 5MB) {
        Move-Item -LiteralPath $script:LogPath -Destination "$($script:LogPath).1" -Force
    }
    $line = '{0} run={1} mode={2} caller={3} helper_pid={4} elapsed_ms={5} {6}' -f `
        [DateTime]::UtcNow.ToString('o'), $script:RunId, $Mode, $Caller, $PID, $script:RunTimer.ElapsedMilliseconds, $Message
    Add-Content -LiteralPath $script:LogPath -Value $line -Encoding UTF8
}

function Test-Http200 {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [int]$TimeoutSeconds = 4
    )
    if (-not ('System.Net.Http.HttpClientHandler' -as [type])) {
        Add-Type -AssemblyName System.Net.Http
    }
    $handler = New-Object System.Net.Http.HttpClientHandler
    $handler.UseProxy = $false
    $client = New-Object System.Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    try {
        $response = $client.GetAsync($Uri).GetAwaiter().GetResult()
        return ([int]$response.StatusCode -eq 200)
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $client) { $client.Dispose() }
        if ($null -ne $handler) { $handler.Dispose() }
    }
}

function Test-Healthy {
    return (Test-Http200 -Uri 'http://127.0.0.1:1933/health' -TimeoutSeconds 4)
}

function Test-MaintenanceActive {
    if (-not (Test-Path -LiteralPath $MaintenanceFlag)) { return $false }
    try {
        return ((Get-Content -LiteralPath $MaintenanceFlag -Raw).Trim() -eq 'active')
    }
    catch {
        return $true
    }
}

function Get-TargetProcesses {
    $all = @(Get-CimInstance Win32_Process)
    $selected = @{}
    foreach ($process in $all) {
        $hasConfig = $false
        if ($process.CommandLine) {
            $hasConfig = $process.CommandLine.IndexOf($ConfigPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        }
        $isServerExe = $process.ExecutablePath -and ($process.ExecutablePath -ieq $ServerExe)
        $isConfiguredPython = ($process.Name -ieq 'python.exe') -and $hasConfig -and
            ($process.CommandLine.IndexOf('--config', [StringComparison]::OrdinalIgnoreCase) -ge 0)
        if ($isServerExe -or $isConfiguredPython) {
            $selected[[int]$process.ProcessId] = $process
        }
    }

    $changed = $true
    while ($changed) {
        $changed = $false
        foreach ($process in $all) {
            $id = [int]$process.ProcessId
            $parentId = [int]$process.ParentProcessId
            if (($selected.ContainsKey($parentId)) -and (-not $selected.ContainsKey($id))) {
                $selected[$id] = $process
                $changed = $true
            }
        }
    }
    return @($selected.Values)
}

function Get-ListenerOwners {
    $connections = @(Get-NetTCPConnection -LocalPort 1933 -State Listen -ErrorAction SilentlyContinue)
    return @($connections | ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
}

function Get-RecoveryState {
    if (Test-Path -LiteralPath $StatePath) {
        try {
            return (Get-Content -LiteralPath $StatePath -Raw | ConvertFrom-Json)
        }
        catch {
            Write-RecoveryLog 'WARN state file could not be parsed; starting a fresh recovery incident'
        }
    }
    $incident = Get-Date -Format 'yyyyMMdd-HHmmssfff'
    return [pscustomobject]@{
        IncidentId = $incident
        Attempts = 0
        NextAttemptUtc = ''
        DataBackupComplete = $false
        DataBackupPath = ''
    }
}

function Save-RecoveryState {
    param([Parameter(Mandatory = $true)]$State)
    $parent = Split-Path -Parent $StatePath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $temporary = "$StatePath.tmp"
    $json = $State | ConvertTo-Json -Depth 4
    [System.IO.File]::WriteAllText($temporary, $json, [System.Text.Encoding]::UTF8)
    Move-Item -LiteralPath $temporary -Destination $StatePath -Force
}

function Get-TreeDepth {
    param(
        [Parameter(Mandatory = $true)]$Process,
        [Parameter(Mandatory = $true)][hashtable]$ById
    )
    $depth = 0
    $parentId = [int]$Process.ParentProcessId
    while ($ById.ContainsKey($parentId) -and $depth -lt 64) {
        $depth++
        $parentId = [int]$ById[$parentId].ParentProcessId
    }
    return $depth
}

function Stop-TargetProcesses {
    $targets = @(Get-TargetProcesses)
    $targetIds = @($targets | ForEach-Object { [int]$_.ProcessId })
    $owners = @(Get-ListenerOwners)
    $unrelatedOwners = @($owners | Where-Object { $targetIds -notcontains $_ })
    if ($unrelatedOwners.Count -gt 0) {
        throw ('port 1933 is owned by an unrelated process: ' + ($unrelatedOwners -join ','))
    }

    if ($targets.Count -eq 0) {
        Write-RecoveryLog 'INFO no matching OpenViking process found'
    }
    else {
        $byId = @{}
        foreach ($process in $targets) { $byId[[int]$process.ProcessId] = $process }
        $ordered = @($targets | Sort-Object @{ Expression = { Get-TreeDepth -Process $_ -ById $byId }; Descending = $true })
        foreach ($process in $ordered) {
            Write-RecoveryLog ('INFO stopping verified OpenViking pid={0} image={1}' -f $process.ProcessId, $process.Name)
            Stop-Process -Id ([int]$process.ProcessId) -Force -ErrorAction Stop
        }
    }

    $deadline = (Get-Date).AddSeconds(25)
    do {
        Start-Sleep -Milliseconds 500
        $remaining = @(Get-TargetProcesses)
        $owners = @(Get-ListenerOwners)
        if (($remaining.Count -eq 0) -and ($owners.Count -eq 0)) { return }
    } while ((Get-Date) -lt $deadline)

    $remainingIds = @($remaining | ForEach-Object { [int]$_.ProcessId })
    $owners = @(Get-ListenerOwners)
    throw ('OpenViking exit not confirmed; remaining pids={0}; listener owners={1}' -f ($remainingIds -join ','), ($owners -join ','))
}

function Backup-ColdData {
    param([Parameter(Mandatory = $true)]$State)
    if ([bool]$State.DataBackupComplete) { return }
    if (-not (Test-Path -LiteralPath $DataPath -PathType Container)) {
        throw 'OpenViking data directory is missing; refusing to launch without a cold backup'
    }

    $incidentRoot = Join-Path $DataBackupRoot ([string]$State.IncidentId)
    $staging = Join-Path $incidentRoot 'data.partial'
    $final = Join-Path $incidentRoot 'data'
    New-Item -ItemType Directory -Path $incidentRoot -Force | Out-Null
    if (Test-Path -LiteralPath $final -PathType Container) {
        throw 'cold backup destination already exists but is not marked complete'
    }
    if (Test-Path -LiteralPath $staging) {
        $resolvedRoot = [System.IO.Path]::GetFullPath($incidentRoot).TrimEnd('\') + '\'
        $resolvedStaging = [System.IO.Path]::GetFullPath($staging)
        if (-not $resolvedStaging.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'cold backup staging path escaped its configured root'
        }
        Remove-Item -LiteralPath $staging -Recurse -Force
    }

    $before = Get-ChildItem -LiteralPath $DataPath -Recurse -File | Measure-Object -Property Length -Sum
    $robocopy = Join-Path $env:WINDIR 'System32\robocopy.exe'
    $copyLog = Join-Path $incidentRoot 'robocopy.log'
    & $robocopy $DataPath $staging /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /XJ /NFL /NDL /NP /NJH /NJS "/LOG:$copyLog"
    $copyCode = $LASTEXITCODE
    if ($copyCode -ge 8) { throw "cold data backup failed; robocopy exit code $copyCode" }

    $after = Get-ChildItem -LiteralPath $staging -Recurse -File | Measure-Object -Property Length -Sum
    if (($before.Count -ne $after.Count) -or ($before.Sum -ne $after.Sum)) {
        throw ('cold data backup verification mismatch; source files={0} bytes={1}; backup files={2} bytes={3}' -f $before.Count, $before.Sum, $after.Count, $after.Sum)
    }
    Move-Item -LiteralPath $staging -Destination $final
    $State.DataBackupComplete = $true
    $State.DataBackupPath = $final
    Save-RecoveryState -State $State
    Write-RecoveryLog ('INFO cold data backup verified; files={0}; bytes={1}; robocopy_rc={2}' -f $after.Count, $after.Sum, $copyCode)
}

function Start-OpenViking {
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmssfff'
    $stdout = Join-Path $LogRoot "server-$stamp.stdout.log"
    $stderr = Join-Path $LogRoot "server-$stamp.stderr.log"
    $process = Start-Process -FilePath $ServerExe -ArgumentList @('--config', $ConfigPath) `
        -WorkingDirectory $ServerRoot -WindowStyle Hidden `
        -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    Write-RecoveryLog ('INFO launched OpenViking pid={0}; stdout={1}; stderr={2}' -f $process.Id, $stdout, $stderr)

    for ($attempt = 1; $attempt -le 36; $attempt++) {
        Start-Sleep -Seconds 5
        if (Test-Healthy) {
            Write-RecoveryLog ('OK /health returned 200; pid={0}; elapsed_seconds={1}' -f $process.Id, ($attempt * 5))
            return $true
        }
        $process.Refresh()
        if ($process.HasExited) {
            Write-RecoveryLog ('FAIL launched process exited before /health; pid={0}; exit_code={1}; stderr={2}' -f $process.Id, $process.ExitCode, $stderr)
            return $false
        }
    }
    Write-RecoveryLog ('FAIL startup health timeout after 180 seconds; pid={0}; stdout={1}; stderr={2}' -f $process.Id, $stdout, $stderr)
    return $false
}

$exitCode = 0
$mutex = $null
$locked = $false
$stage = 'preflight'
try {
    $selfRow = Get-CimInstance Win32_Process -Filter "ProcessId=$PID"
    Write-RecoveryLog ('BEGIN parent_pid={0} stage=preflight' -f $selfRow.ParentProcessId)
    if ($Mode -eq 'Ensure' -and (Test-MaintenanceActive)) {
        Write-RecoveryLog 'SKIP maintenance'
        return
    }
    if (-not (Test-Path -LiteralPath $ServerExe -PathType Leaf)) { throw 'OpenViking executable is missing' }
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { throw 'OpenViking config is missing' }

    $mutex = New-Object System.Threading.Mutex($false, $MutexName)
    Write-RecoveryLog 'INFO stage=lock_wait'
    try { $locked = $mutex.WaitOne(60000) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'timed out waiting for the OpenViking recovery mutex' }
    Write-RecoveryLog 'INFO stage=lock_acquired'

    if ($Mode -eq 'Ensure' -and (Test-Healthy)) {
        $owners = @(Get-ListenerOwners)
        Write-RecoveryLog ('OK stage=healthy listener_pids={0}' -f ($owners -join ','))
        if (Test-Path -LiteralPath $StatePath) { Remove-Item -LiteralPath $StatePath -Force }
        return
    }

    $state = Get-RecoveryState
    if ($Mode -eq 'Ensure' -and $state.NextAttemptUtc) {
        $nextAttempt = [DateTime]::MinValue
        if ([DateTime]::TryParse([string]$state.NextAttemptUtc, [ref]$nextAttempt) -and ((Get-Date).ToUniversalTime() -lt $nextAttempt.ToUniversalTime())) {
            Write-RecoveryLog ('SKIP backoff next_utc={0}' -f $nextAttempt.ToUniversalTime().ToString('o'))
            return
        }
    }
    $stage = 'stop'
    Write-RecoveryLog 'INFO stage=stop'
    Stop-TargetProcesses
    $stage = 'cold-backup'
    Write-RecoveryLog 'INFO stage=cold-backup'
    Backup-ColdData -State $state
    $stage = 'launch'
    Write-RecoveryLog 'INFO stage=launch'
    $startedHealthy = Start-OpenViking
    if (-not $startedHealthy) { throw 'OpenViking did not become healthy within 180 seconds' }
    if (Test-Path -LiteralPath $StatePath) { Remove-Item -LiteralPath $StatePath -Force }
}
catch {
    $exitCode = 1
    try {
        if ($null -ne $state) {
            $state.Attempts = [int]$state.Attempts + 1
            $delayMinutes = [Math]::Min(30, 2 * [Math]::Pow(2, [Math]::Min(4, [int]$state.Attempts - 1)))
            $state.NextAttemptUtc = (Get-Date).ToUniversalTime().AddMinutes($delayMinutes).ToString('o')
            Save-RecoveryState -State $state
        }
    }
    catch { }
    try { Write-RecoveryLog ('FAIL stage={0}; type={1}; detail={2}' -f $stage, $_.Exception.GetType().Name, $_.Exception.Message) }
    catch { }
}
finally {
    try { Write-RecoveryLog ('END stage={0} exit={1}' -f $stage, $exitCode) } catch { }
    if ($locked -and ($null -ne $mutex)) {
        try { $mutex.ReleaseMutex() } catch { }
    }
    if ($null -ne $mutex) { $mutex.Dispose() }
}

exit $exitCode
