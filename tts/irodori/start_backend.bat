@echo off
chcp 65001 >nul
rem Local overrides (repo-root config/local.env) - real environment variables win.
call "%~dp0..\..\launch\load_env.bat"
rem 2026-10-30: keep the ja text of each rendered block in the log (see render_queue.speech_log)
if not defined TTS_LOG_TEXT set "TTS_LOG_TEXT=1"
cd /d "%~dp0"
echo ============================================
echo  Irodori-TTS backend (5 ships, single process)  :9889
echo  voices: albion / taihou / shinano / brest / jinshi
echo  Speaker-Inversion embedding (s500) + caption-LoRA (taihou: no LoRA)
echo  emotion/style is decided by the backend, no need for the caller
echo  first load takes ~15s (then resident, 2-7s per sentence)
echo  once you see this line the bridge may start; closing this window stops it
echo ============================================
echo.
rem Controlled voice caption: on by default, 2026-10-26.
if not defined ALBION_VOICE_CAPTION set "ALBION_VOICE_CAPTION=1"
"%~dp0.venv\Scripts\python.exe" "%~dp0irodori_serve.py"
if not defined ALBION_SILENT pause
