@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo  Albion TTS bridge (OpenAI-compatible /v1/audio/speech)  :9881
echo  voices: albion / taihou / shinano / brest / jinshi
echo  requires: Irodori backend (:9889) running
echo  closing this window stops the bridge
echo ============================================
echo.
rem Translation (zh -> ja): official DeepSeek API, thinking off (bridge default), 2026-09-23.
if not defined TTS_TRANSLATE_BASE set "TTS_TRANSLATE_BASE=https://api.deepseek.com/v1"
if not defined TTS_TRANSLATE_MODEL set "TTS_TRANSLATE_MODEL=deepseek-flash"
rem Controlled voice caption (per-sentence sound description) + sentence-final emoji: on by default, 2026-10-26.
if not defined ALBION_VOICE_CAPTION set "ALBION_VOICE_CAPTION=1"
if not defined ALBION_VOICE_EMOJI set "ALBION_VOICE_EMOJI=1"
"%~dp0.venv\Scripts\python.exe" bridge.py
if not defined ALBION_SILENT pause
