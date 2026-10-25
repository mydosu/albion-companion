@echo off
rem 双击时隐藏总启动窗口；show 参数保留前台进度。
if "%~1"=="" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" -Bat "%~dp0start_all.bat" -BatArgs "exe __hidden"
  exit /b %errorlevel%
)
call "%~dp0start_all.bat" exe
exit /b %errorlevel%
