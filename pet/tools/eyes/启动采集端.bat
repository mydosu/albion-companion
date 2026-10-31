@echo off
chcp 936 >nul
cd /d "%~dp0"

rem 双击时用无控制台方式重跑自己一次（不留黑窗口）
if /i not "%~1"=="-hidden" (
  if exist "%~dp0_静默.vbs" (
    wscript //nologo "%~dp0_静默.vbs" "%~f0"
    exit /b 0
  )
  echo [%date% %time%] 警告: 同目录缺少 _静默.vbs, 这次会有窗口 >>"%~dp0眼睛.log"
)

rem 日志文件（旧实例可能还攥着它；打不开就换 眼睛2.log，保证下面那条命令行不会被放弃）
set "LOG=%~dp0眼睛.log"
( echo. >>"%LOG%" ) 2>nul || set "LOG=%~dp0眼睛2.log"

rem 已经在跑就什么都不做（绝不再"先杀再起" —— 那正是之前把好的杀掉的元凶）
netstat -ano | findstr ":8790" | findstr "LISTENING" >nul 2>nul
if not errorlevel 1 (
  echo [%date% %time%] 8790 已经在跑, 什么都不做 >>"%LOG%" 2>nul
  exit /b 0
)

rem 等 Tailscale 就绪（开机那一刻可能还没起来），最多 30 秒
set "TS="
for /l %%i in (1,1,15) do (
  if not defined TS (
    for /f "delims=" %%T in ('powershell -NoProfile -Command "if (Test-Path 'C:\Program Files\Tailscale\tailscale.exe') { & 'C:\Program Files\Tailscale\tailscale.exe' ip -4 } else { tailscale ip -4 }" 2^>nul') do set "TS=%%T"
    if not defined TS ping -n 3 127.0.0.1 >nul
  )
)
echo [%date% %time%] ---- 启动 ---- tailscale=%TS% >>"%LOG%" 2>nul

rem token 从同目录 token.txt 读（一台一份，不写死在脚本里）
set "TOK="
if exist "%~dp0token.txt" for /f "usebackq delims=" %%A in ("%~dp0token.txt") do if not defined TOK set "TOK=%%A"
if not defined TOK (
  echo [%date% %time%] 缺 token.txt, 退出 >>"%LOG%" 2>nul
  exit /b 1
)

rem 找 Python
set "PY="
rem 先找装好的真 python.exe：py / python3 会经微软商店的转发入口再起一层，进程里就成了三个（2026-10-14）
rem 通配符只能在最后一段，目录名带 * 要用 for /d 先列目录（原写法从来没匹配上过）
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*" "%ProgramFiles%\Python3*" "C:\Python3*") do if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
if not defined PY ( where py >nul 2>nul && set "PY=py" )
if not defined PY ( where python >nul 2>nul && set "PY=python" )
if not defined PY ( where python3 >nul 2>nul && set "PY=python3" )
if not defined PY (
  for %%P in ("%LOCALAPPDATA%\Programs\Python\Python3*\python.exe" "%ProgramFiles%\Python3*\python.exe" "C:\Python3*\python.exe") do if exist "%%~fP" set "PY=%%~fP"
)
if not defined PY (
  echo [%date% %time%] 没找到 Python >>"%LOG%" 2>nul
  exit /b 1
)

set "PYTHONUNBUFFERED=1"
rem --lan：同一局域网时直连（照 MWB 的做法），不走 Tailscale 中继；仍要 token
rem token 走 --token-file 从 token.txt 读，不进进程命令行（值以 - 开头也不会被 argparse 误当选项）
"%PY%" capture_agent.py --port 8790 --token-file "%~dp0token.txt" --max-width 0 --lan >>"%LOG%" 2>&1
exit /b 0
