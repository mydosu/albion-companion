$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
. (Join-Path $repo 'tools\openviking-observe.ps1') -Library
$passed = 0
foreach ($name in @('openviking-recover.ps1', 'openviking-observe.ps1')) {
    $tokens = $null; $parseErrors = $null
    $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo "tools\$name"), [ref]$tokens, [ref]$parseErrors)
    if (@($parseErrors).Count -ne 0) { throw "parse failed: $name" }
    $passed++
}
# A process owned by this test exits normally with a chosen status. No service injection.
$process = Start-Process -FilePath 'D:\AI\openviking\venv\Scripts\python.exe' `
    -ArgumentList @('-c', '"import time; time.sleep(2); raise SystemExit(17)"') `
    -WindowStyle Hidden -PassThru
$row = Get-CimInstance Win32_Process -Filter "ProcessId=$($process.Id)"
$entry = Open-ObservedProcess $row
try {
    if ($null -ne (Read-ObservedExit $entry)) { throw 'test process exited too early' }
    $passed++
    $bad = [pscustomobject]@{ ProcessId = $row.ProcessId; CreationDate = $row.CreationDate.AddSeconds(-10) }
    $rejected = $false
    try { $null = Open-ObservedProcess $bad } catch { $rejected = $true }
    if (-not $rejected) { throw 'PID reuse identity was accepted' }
    $passed++
    $process.WaitForExit()
    $event = Read-ObservedExit $entry
    if ($event.exit_code -ne 17 -or $event.exit_hex -ne '0x00000011') { throw 'exit status not retained' }
    $passed++
}
finally {
    $entry.Process.Dispose()
    $process.Dispose()
}
Write-Output "observer_checks_pass=$passed fail=0"
