param(
    [Parameter(Mandatory=$true, ParameterSetName='Command')][string]$Cmd,
    [Parameter(Mandatory=$true, ParameterSetName='Batch')][string]$Bat,
    [Parameter(ParameterSetName='Batch')][string]$BatArgs,
    [string]$Log,
    [switch]$Visible
)
if ($PSCmdlet.ParameterSetName -eq 'Batch') {
    $Cmd = 'call "' + (Resolve-Path -LiteralPath $Bat).Path + '" ' + $BatArgs
}
if ($Log) {
    [void][System.IO.Directory]::CreateDirectory((Split-Path -Parent ([System.IO.Path]::GetFullPath($Log))))
    $Cmd += ' >> "' + $Log + '" 2>&1'
}
if ($Visible) {
    Start-Process -FilePath $env:ComSpec -ArgumentList ('/d /k ' + $Cmd)
    return
}
# CreateNoWindow avoids a console even with Windows Terminal as the default host.
$si = New-Object System.Diagnostics.ProcessStartInfo
$si.FileName = $env:ComSpec
$si.Arguments = '/d /c ' + $Cmd
$si.UseShellExecute = $false
$si.CreateNoWindow = $true
# Nobody can press a key in a hidden window: the `if not defined ALBION_SILENT pause` tail of
# start_*.bat would freeze the service forever (2026-11-28: restarting 9881 this way left the outer
# cmd sitting on pause, port never came up). Set it here so the plain `-Bat <start_*.bat>` restart
# path is safe by itself. (-Visible is untouched: a human is watching that one.)
# ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less files as ANSI.
$si.EnvironmentVariables['ALBION_SILENT'] = '1'
[void][System.Diagnostics.Process]::Start($si)
