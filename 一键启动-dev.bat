@echo off
rem 开发版沿用后台启动。
call "%~dp0start_all.bat" dev
exit /b %errorlevel%
