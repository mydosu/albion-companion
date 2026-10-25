@echo off
chcp 936 >nul
title 阿尔比恩 · 一键关闭
setlocal enabledelayedexpansion
set SYS=%SystemRoot%\System32

echo ==========================================
echo   阿尔比恩桌宠 · 一键关闭
echo   （按端口找主人，找到就关；找不到就跳过）
echo ==========================================
echo.

for %%P in (9889 9881 9885 9882 8650 8790 9335) do (
  call :killport %%P
)

echo ---- 停掉沙箱里的脑（WSL：/root/albion-pi/stop.sh，停 :8650 与 :8798）----
wsl -d Ubuntu -- bash /root/albion-pi/stop.sh && echo   已关笼子里的脑 || echo   脑本来就没在跑
echo.

taskkill /IM airi.exe /F /T >nul 2>nul && echo   已关 airi.exe ^(打包版壳^)

echo.
echo ---- 收残留的控制台窗口 ----
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_收控制台.ps1"

echo.
echo ---- 关闭后核对 ----
for %%P in (9889 9881 9885 9882 8650 8790 9335) do (
  call :showport %%P
)
echo.
echo 房间收拾干净了。要再开，双击 一键启动-构建版.bat。
echo.
pause
exit /b

:killport
set FOUND=0
for /f "tokens=5" %%A in ('%SYS%\netstat.exe -ano ^| %SYS%\findstr.exe ":%~1 " ^| %SYS%\findstr.exe LISTENING') do (
  if not "%%A"=="0" (
    taskkill /PID %%A /F /T >nul 2>nul
    if !errorlevel!==0 (echo   已关 %~1 ^(PID %%A^) & set FOUND=1)
  )
)
if "!FOUND!"=="0" echo   %~1 本来就没在跑
exit /b

:showport
%SYS%\netstat.exe -ano | %SYS%\findstr.exe ":%~1 " | %SYS%\findstr.exe LISTENING >nul
if !errorlevel!==0 (echo   %~1 仍在监听 - 需要手动看一下) else (echo   %~1 已释放)
exit /b
