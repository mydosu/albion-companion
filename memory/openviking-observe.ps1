[CmdletBinding()]
param(
    [switch]$Once,
    [switch]$Library,
    [string]$LogRoot = 'D:\AI\scripts\logs\openviking-recovery'
)

$ErrorActionPreference = 'Stop'
$ConfigPath = 'D:\AI\openviking\ov.conf'
$ServerExe = 'D:\AI\openviking\venv\Scripts\openviking-server.exe'
$ObserverLog = Join-Path $LogRoot 'process-exits.jsonl'

function Write-ObserverEvent {
    param([hashtable]$Fields)
    New-Item -ItemType Directory -Path $LogRoot -Force | Out-Null
    if ((Test-Path -LiteralPath $ObserverLog) -and (Get-Item -LiteralPath $ObserverLog).Length -gt 5MB) {
        Move-Item -LiteralPath $ObserverLog -Destination "$ObserverLog.1" -Force
    }
    $Fields.utc = [DateTime]::UtcNow.ToString('o')
    $Fields.observer_pid = $PID
    Add-Content -LiteralPath $ObserverLog -Value ($Fields | ConvertTo-Json -Compress) -Encoding UTF8
}

function Get-VerifiedSnapshot {
    $all = @(Get-CimInstance Win32_Process)
    $selected = @{}
    foreach ($row in $all) {
        $matchesConfig = $row.CommandLine -and $row.CommandLine.IndexOf($ConfigPath, [StringComparison]::OrdinalIgnoreCase) -ge 0
        if (($row.ExecutablePath -ieq $ServerExe) -or
            (($row.Name -ieq 'python.exe') -and $matchesConfig -and $row.CommandLine.Contains('--config'))) {
            $selected[[int]$row.ProcessId] = $row
        }
    }
    do {
        $added = $false
        foreach ($row in $all) {
            if ($selected.ContainsKey([int]$row.ParentProcessId) -and -not $selected.ContainsKey([int]$row.ProcessId)) {
                $selected[[int]$row.ProcessId] = $row
                $added = $true
            }
        }
    } while ($added)
    return @($selected.Values)
}

function Open-ObservedProcess {
    param($Row)
    $process = [System.Diagnostics.Process]::GetProcessById([int]$Row.ProcessId)
    # Acquire a handle while alive, to retain ExitCode after the PID disappears.
    $null = $process.Handle
    $created = $process.StartTime.ToUniversalTime().ToString('o')
    # Reject a PID reused between the CIM snapshot and opening its handle.
    if ([Math]::Abs(($process.StartTime.ToUniversalTime() - $Row.CreationDate.ToUniversalTime()).TotalMilliseconds) -gt 2) {
        $process.Dispose()
        throw 'process identity changed'
    }
    return @{ Process = $process; Key = "$($Row.ProcessId)/$created"; Id = [int]$Row.ProcessId;
              ParentId = [int]$Row.ParentProcessId; Created = $created; Name = [string]$Row.Name }
}

function Read-ObservedExit {
    param([hashtable]$Entry)
    $process = $Entry.Process
    if (-not $process.HasExited) { return $null }
    try {
        $code = $process.ExitCode
        return @{ event = 'exit'; pid = $Entry.Id; parent_pid = $Entry.ParentId;
                  created_utc = $Entry.Created; image = $Entry.Name; exit_code = $code;
                  exit_hex = ('0x{0:X8}' -f $code); cause = 'undetermined' }
    }
    catch {
        return @{ event = 'exit'; pid = $Entry.Id; created_utc = $Entry.Created;
                  exit_code = 'unknown'; error_type = $_.Exception.GetType().Name }
    }
}

if ($Library) { return }
$mutex = New-Object System.Threading.Mutex($false, 'Local\AlbionOpenVikingObserver')
$locked = $false
$tracked = @{}
try {
    try { $locked = $mutex.WaitOne(0) }
    catch [System.Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { return }
    Write-ObserverEvent @{ event = 'observer_start' }
    $nextSnapshot = [DateTime]::MinValue
    $nextHeartbeat = [DateTime]::MinValue
    do {
        foreach ($key in @($tracked.Keys)) {
            $entry = $tracked[$key]
            $exited = Read-ObservedExit -Entry $entry
            if ($null -ne $exited) {
                Write-ObserverEvent $exited
                $entry.Process.Dispose()
                $tracked.Remove($key)
            }
        }
        if ([DateTime]::UtcNow -ge $nextSnapshot) {
            $nextSnapshot = [DateTime]::UtcNow.AddSeconds(10)
            foreach ($row in @(Get-VerifiedSnapshot)) {
                $already = @($tracked.Values | Where-Object { $_.Id -eq [int]$row.ProcessId })
                if ($already.Count -gt 0) { continue }
                try {
                    $entry = Open-ObservedProcess $row
                    $tracked[$entry.Key] = $entry
                    Write-ObserverEvent @{ event = 'attach'; pid = $entry.Id; parent_pid = $entry.ParentId;
                                           image = $entry.Name; created_utc = $entry.Created }
                }
                catch {
                    Write-ObserverEvent @{ event = 'attach_failed'; pid = [int]$row.ProcessId;
                                           error_type = $_.Exception.GetType().Name }
                }
            }
        }
        if ([DateTime]::UtcNow -ge $nextHeartbeat) {
            Write-ObserverEvent @{ event = 'heartbeat'; tracked = $tracked.Count }
            $nextHeartbeat = [DateTime]::UtcNow.AddMinutes(5)
        }
        if (-not $Once) { Start-Sleep -Seconds 1 }
    } while (-not $Once)
}
catch {
    Write-ObserverEvent @{ event = 'observer_failed'; error_type = $_.Exception.GetType().Name }
    exit 1
}
finally {
    foreach ($entry in $tracked.Values) { $entry.Process.Dispose() }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
