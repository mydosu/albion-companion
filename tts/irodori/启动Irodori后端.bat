@echo off
chcp 65001 >nul
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
"%~dp0.venv\Scripts\python.exe" "%~dp0irodori_serve.py"
if not defined ALBION_SILENT pause
