@echo off
setlocal
rem 一键安装入口：只把参数原样转给 tools\setup\install_all.ps1。
rem 建议先演练：一键安装.bat -DryRun
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\setup\install_all.ps1" %*
exit /b %errorlevel%
