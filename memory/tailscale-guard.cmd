@echo off
rem tailscale-guard.cmd - WARP/Tailscale conflict self-heal guard
rem Triggered by Task Scheduler every 2 minutes.
rem If Tailscale backend is Stopped -> tailscale up + restart HermesHub
rem If Tailscale running but Hub port 9800 dead -> restart HermesHub
setlocal

set "HUB_VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\HermesHub.vbs"

rem ---- 1. Check Tailscale backend state ----
set "TS_STATE=unknown"
for /f "usebackq delims=" %%S in (`tailscale status --json 2^>nul`) do (
    echo %%S | findstr /c:"\"BackendState\": \"Running\"" >nul
    if not errorlevel 1 set "TS_STATE=running"
)

rem ---- 0. claude-code-cache-fix proxy 9801：已按用户要求取消（2026-09-18）----
rem 要用 claude / codex 的时候手动起：cscript //nologo "D:\AI\claude-cache-fix\start-cache-fix.vbs"

rem ---- 0b. 记忆提取健康检查（服务活着≠提取在工作，2026-09 曾静默失败两天）----
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "D:\AI\scripts\openviking-recover.ps1" -Mode Ensure -Caller TailscaleGuard
if errorlevel 1 echo [%date% %time%] openviking recovery failed; inspect logs\openviking-recovery
"D:\AI\openviking\venv\Scripts\python.exe" "D:\AI\scripts\openviking-memory-health.py" >nul 2>&1

if "%TS_STATE%"=="running" (
    rem ---- Tailscale OK. Re-apply openviking plugin patch if missing ----
    call "%~dp0patch-openviking-plugin.cmd"

    rem ---- Check Hub port ----
    netstat -ano | findstr ":9800.*LISTENING" >nul
    if errorlevel 1 (
        echo [%date% %time%] Hub 9800 not listening, restarting
        if exist "%HUB_VBS%" cscript //nologo "%HUB_VBS%"
    )

    rem ---- llama 8082 intentionally disabled; do not probe or start ----
    rem This remains disabled even if a legacy Startup VBS reappears.
    rem ---- Skill curator: run daily audit + deliver to fixed feed session ----
    rem (skill-curator-notify.py skips itself if already ran today)
    echo [%date% %time%] skill curator daily check
    start "" /b wscript.exe "D:\AI\scripts\skill-curator-runner.vbs"
    goto :done
)

echo [%date% %time%] Tailscale backend STOPPED - recovering
tailscale up
timeout /t 8 /nobreak >nul
if exist "%HUB_VBS%" cscript //nologo "%HUB_VBS%"
echo [%date% %time%] Tailscale + Hub recovered

:done
endlocal
