$pat = '阿尔比恩|启动TTS桥|启动Irodori|start_ear|capture_agent|bridge\.py|irodori_serve|whisper_serve|electron-vite'
$killed = 0
Get-Process | Where-Object { $_.MainWindowTitle -and $_.MainWindowTitle -match $pat } | ForEach-Object {
  taskkill /PID $_.Id /T /F 2>$null | Out-Null
  Write-Output ("  收掉 " + $_.ProcessName + " (PID " + $_.Id + ") 窗口: " + $_.MainWindowTitle)
  $script:killed++
}
if ($script:killed -eq 0) { Write-Output "  没有残留的控制台窗口" }
