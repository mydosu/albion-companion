@echo off
chcp 936 >nul
cd /d "%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\阿尔比恩的眼睛.vbs"

rem 自启只干一件事：调用和手动双击完全同一个脚本（参数只有一处维护）
> "%VBS%" echo Set sh = CreateObject("WScript.Shell")
>>"%VBS%" echo sh.CurrentDirectory = "%~dp0."
>>"%VBS%" echo sh.Run """%~dp0启动采集端.bat""", 0, False
echo 已写入自启: %VBS%
echo.

rem 检查一下现在跑没跑（不杀、不动任何进程）
netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo 8790 已经在跑, 不去动它。想重启就先双击"关闭采集端.bat", 再双击"启动采集端.bat"。
  echo.
  pause
  exit /b 0
)
echo 现在没在跑, 顺手起一次...
call "%~dp0启动采集端.bat" -hidden
ping -n 4 127.0.0.1 >nul
echo.
echo 探活（走 Tailscale 地址，探 127.0.0.1 会误判）...
set "TS="
for /f "delims=" %%T in ('powershell -NoProfile -Command "if (Test-Path 'C:\Program Files\Tailscale\tailscale.exe') { & 'C:\Program Files\Tailscale\tailscale.exe' ip -4 } else { tailscale ip -4 }" 2^>nul') do set "TS=%%T"
if not defined TS ( echo   取不到 Tailscale 地址, 跳过探活 & pause & exit /b 0 )
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 8 -Uri ('http://' + '%TS%' + ':8790/health'); Write-Host ('   [成功] 采集端已就绪: ' + $r.Content) } catch { Write-Host '   [失败] 探活没通过 —— 把下面几行发我'; foreach ($f in '%~dp0眼睛.log','%~dp0眼睛2.log') { if (Test-Path $f) { Write-Host ('--- ' + $f + ' ---'); Get-Content $f -Tail 12 } } }"
echo.
pause
exit /b 0
