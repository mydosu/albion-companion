@echo off
chcp 936 >nul
title 阿尔比恩 · 一键启动（构建版）
setlocal enabledelayedexpansion
set SYS=%SystemRoot%\System32
for %%I in ("%~dp0.") do set "WIFE=%%~fI"
set "SHELL_MODE=exe"
if /i "%~1"=="dev" set "SHELL_MODE=dev"
set "CONSOLE_FLAG="
if "%ALBION_SHOW_CONSOLES%"=="1" set "CONSOLE_FLAG=-Visible"
if not defined CONSOLE_FLAG set "ALBION_SILENT=1"
if not exist "%WIFE%\logs" mkdir "%WIFE%\logs"

echo ==========================================
echo   阿尔比恩桌宠 · 一键启动（构建版）
echo   （已经在跑的服务会自动跳过，不会重复起）
echo ==========================================
echo.

rem 1) TTS 后端 Irodori（五船单进程，加载约 15 秒）
call :check 9889
if !PORTFREE!==1 (
  echo [1/6] 启动 TTS 后端 :9889 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" !CONSOLE_FLAG! -Bat "%WIFE%\tts\irodori\start_backend.bat" -Log "%WIFE%\logs\irodori.log"
  set NEED_BRIDGE=1
) else (
  echo [1/6] TTS 后端已在运行, 跳过
)

rem 2) TTS 桥（必须等后端就绪）
call :check 9881
if !PORTFREE!==1 (
  echo [2/6] 启动 TTS 桥 :9881 ...
  if "!NEED_BRIDGE!"=="1" (
    echo       等待后端 10 秒 ...
    ping -n 11 127.0.0.1 >nul
  )
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" !CONSOLE_FLAG! -Bat "%WIFE%\tts\irodori\start_bridge.bat" -Log "%WIFE%\logs\tts_bridge.log"
) else (
  echo [2/6] TTS 桥已在运行, 跳过
)

rem 2.5) 流式语音网关（壳走 unspeech v1 真流式；没它她不出声）
call :check 9885
if !PORTFREE!==1 (
  echo [2.5/6] 启动流式语音 :9885 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" !CONSOLE_FLAG! -Bat "%WIFE%\tts\stream\start_stream.bat" -Log "%WIFE%\logs\stream.log"
) else (
  echo [2.5/6] 流式语音已在运行, 跳过
)

rem 3) 耳朵 STT（paraformer，CPU，加载约 5 秒）
call :check 9882
if !PORTFREE!==1 (
  echo [3/6] 启动耳朵 :9882 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" !CONSOLE_FLAG! -Bat "%WIFE%\pet\start_ear.bat" -Log "%WIFE%\logs\ear.log"
) else (
  echo [3/6] 耳朵已在运行, 跳过
)

rem 4) 脑（WSL 里的 Pi 版，/root/albion-pi；以前的 Hermes 退路已删，要回看见 git 历史）
call :check 8650
if !PORTFREE!==1 (
  echo [4/6] 启动脑（pi）：8650 ...
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_启动脑Pi.ps1"
) else (
  echo [4/6] 脑已在运行, 跳过
)

rem 4.5) 眼睛：本机采集端（截图 + 摄像头，同一个进程，只绑本机）
rem token 从 pet\tools\eyes\token.txt 读第一行（不入库，照同目录 token.txt.example 建）；缺了就不起眼睛，不用任何写死的值
set "EYES_TOK="
if exist "%WIFE%\pet\tools\eyes\token.txt" for /f "usebackq delims=" %%A in ("%WIFE%\pet\tools\eyes\token.txt") do if not defined EYES_TOK set "EYES_TOK=%%A"
call :check 8790
if !PORTFREE!==1 (
  if not defined EYES_TOK (
    echo [5/6] 眼睛采集端没起：缺 %WIFE%\pet\tools\eyes\token.txt（或文件是空的）。照同目录 token.txt.example 建，只写一行 token
    >>"%WIFE%\logs\eyes.err.log" 2>nul echo [%date% %time%] 眼睛采集端没起：缺 pet\tools\eyes\token.txt（或文件是空的）
  ) else (
    echo [5/6] 启动眼睛采集端 :8790 ...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" !CONSOLE_FLAG! -Cmd "cd /d \"%WIFE%\pet\tools\eyes\" && \"%WIFE%\tts\GPT-sovits\runtime\python.exe\" capture_agent.py --port 8790 --token-file \"%WIFE%\pet\tools\eyes\token.txt\" --max-width 1920 --bind 127.0.0.1" -Log "%WIFE%\logs\eyes.out.log"
  )
) else (
  echo [5/6] 眼睛采集端已在运行, 跳过
)

rem 6) 壳：exe/prod 用构建版，dev 用开发版；缺 exe 回退 dev。
call :check 9335
if !PORTFREE!==1 (
  if /i "!SHELL_MODE!"=="exe" if not exist "%WIFE%\shell\apps\stage-tamagotchi\dist\win-unpacked\airi.exe" (
    echo [6/6] 未找到 exe，回退 dev ...
    set "SHELL_MODE=dev"
  )
  if /i "!SHELL_MODE!"=="dev" (
    echo [6/6] 启动壳 dev ...
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0_silent_run.ps1" !CONSOLE_FLAG! -Cmd "cd /d \"%WIFE%\shell\apps\stage-tamagotchi\" && npx --yes electron-vite dev -- --remote-debugging-port=9335" -Log "%WIFE%\logs\shell_dev.log"
  ) else (
    echo [6/6] 启动壳 exe ...
    start "" "%WIFE%\shell\apps\stage-tamagotchi\dist\win-unpacked\airi.exe" --remote-debugging-port=9335
  )
) else (
  echo [6/6] 壳已在跑，跳过
)

rem 状态挂件：自己占 :47654 单实例，重复启动会自己退出
wscript "%~dp0pet\widget\AlbionStatus.vbs"

echo ---- 等模型加载, 然后自检（最多 90 秒）----
"%WIFE%\tts\GPT-sovits\runtime\python.exe" "%WIFE%\pet\check_all.py" 90

echo.
echo 服务都在后台静默跑; 想全关掉, 双击 一键关闭.bat.
echo 单独启动: 进 tts\irodori 或 pet 目录双击对应的 .bat.
echo 可选: 想用 Denia 音色, 再双击 tts\GPT-sovits\启动Denia后端.bat (:9880).
echo.
endlocal
exit /b

rem ---- 子过程: 探端口, PORTFREE=1 表示空闲(需要启动) ----
:check
set PORTFREE=1
"%SYS%\netstat.exe" -ano | "%SYS%\findstr.exe" ":%1 " | "%SYS%\findstr.exe" LISTENING >nul
if !errorlevel!==0 set PORTFREE=0
exit /b
