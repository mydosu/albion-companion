@echo off
title 阿尔比恩的耳朵 (paraformer-zh :9882)
cd /d %~dp0
rem Local overrides (repo-root config/local.env) - real environment variables win.
call "%~dp0..\launch\load_env.bat"
if not defined ALBION_STT_ENGINE set ALBION_STT_ENGINE=paraformer
if not defined ALBION_FUNASR_ROOT set ALBION_FUNASR_ROOT=%~dp0models\modelscope\iic
if not defined ALBION_STT_PORT set ALBION_STT_PORT=9882
echo 启动耳朵（paraformer-zh, CPU）...
if not exist "%~dp0.venv\Scripts\python.exe" (
  echo 缺 ear\.venv —— 在仓库根执行 uv sync --directory ear（见 ear\README.md），再跑这个 bat
  exit /b 1
)
"%~dp0.venv\Scripts\python.exe" whisper_serve.py
