@echo off
chcp 936 >nul
rem 兼容旧路径，统一转发根入口。
call "%~dp0..\一键启动.bat" %*
exit /b %errorlevel%
