@echo off
title °¢¶û±È¶÷µÄ¶ú¶ä (paraformer-zh :9882)
cd /d %~dp0
set ALBION_STT_ENGINE=paraformer
set ALBION_FUNASR_ROOT=%~dp0models\modelscope\iic
set ALBION_STT_PORT=9882
echo Æô¶¯¶ú¶ä£¨paraformer-zh, CPU£©...
"%~dp0..\tts\GPT-sovits\runtime\python.exe" whisper_serve.py
