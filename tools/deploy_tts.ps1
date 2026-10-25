param(
    [ValidateSet('Plan','Backup','Deploy','Rollback')][string]$Mode = 'Plan',
    [Parameter(Mandatory=$true)][string]$ReleaseRoot,
    [Parameter(Mandatory=$true)][string]$Manifest,
    [string]$BackupRoot,
    [string]$TargetRoot = 'D:\AI\Wife',
    [string]$Python = 'D:\AI\Wife\tts\irodori\.venv\Scripts\python.exe'
)
$ErrorActionPreference = 'Stop'
# File-only operations: callers must drain and stop the exact TTS PIDs before Deploy/Rollback.
if ($Mode -in @('Deploy','Rollback')) {
    # Windows can briefly report the listener after the verified process has exited.
    # Wait for kernel cleanup; never stop any process from this file-only tool.
    for ($taskAttempt=0; $taskAttempt -lt 10; $taskAttempt++) {
        $busy = @(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {
            $_.LocalPort -in @(9881,9885,9889,19881,19885,19889)
        })
        if (-not $busy.Count) { break }
        Start-Sleep -Seconds 1
    }
    if ($busy.Count) { throw 'TTS listeners must be drained and stopped before file replacement.' }
}
$taskArgs = @('-B', (Join-Path $PSScriptRoot 'tts_release.py'), $Mode,
    '--release-root', $ReleaseRoot, '--target-root', $TargetRoot, '--manifest', $Manifest)
if ($BackupRoot) { $taskArgs += @('--backup-root', $BackupRoot) }
& $Python @taskArgs
if ($LASTEXITCODE -ne 0) { throw "TTS release operation failed: $LASTEXITCODE" }
