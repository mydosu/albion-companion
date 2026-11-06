@echo off
rem Entry: dev shell (electron-vite). Background by default.
rem Add show to see one console per service.
if /i "%~1"=="show" set "ALBION_SHOW_CONSOLES=1"
call "%~dp0launch\start_all.bat" dev
exit /b %errorlevel%
