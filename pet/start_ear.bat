@echo off
title °¢¶û±È¶÷µÄ¶ú¶ä (paraformer-zh :9882)
cd /d %~dp0
rem Local overrides (repo-root config/local.env) - real environment variables win.
call "%~dp0..\launch\load_env.bat"
if not defined ALBION_STT_ENGINE set ALBION_STT_ENGINE=paraformer
if not defined ALBION_FUNASR_ROOT set ALBION_FUNASR_ROOT=%~dp0models\modelscope\iic
if not defined ALBION_STT_PORT set ALBION_STT_PORT=9882
echo Æô¶¯¶ú¶ä£¨paraformer-zh, CPU£©...
"%~dp0..\tts\GPT-sovits\runtime\python.exe" whisper_serve.py
