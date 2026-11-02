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
[void][System.Diagnostics.Process]::Start($si)
