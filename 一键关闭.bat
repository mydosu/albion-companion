@echo off
rem Entry: stop everything (ports + WSL brain + shell). Implementation: launch\stop_all.bat
call "%~dp0launch\stop_all.bat" %*
exit /b %errorlevel%
