@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set ALBION_PLAN_VERSION=4
cd /d %~dp0
"..\irodori\.venv\Scripts\python.exe" -u stream_server.py
