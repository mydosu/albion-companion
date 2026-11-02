@echo off
chcp 65001 >nul
rem Local overrides (repo-root config/local.env) - real environment variables win.
call "%~dp0..\..\launch\load_env.bat"
set PYTHONIOENCODING=utf-8
set ALBION_PLAN_VERSION=4
cd /d %~dp0
"..\irodori\.venv\Scripts\python.exe" -u stream_server.py
