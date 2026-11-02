@echo off
rem Entry: build (packaged exe). Hidden by default; progress goes to logs\start_hidden.log.
rem Falls back to `dev` when shell\apps\stage-tamagotchi\dist\win-unpacked\airi.exe is missing.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch\silent_run.ps1" -Bat "%~dp0launch\start_all.bat" -BatArgs "exe __hidden" -Log "%~dp0logs\start_hidden.log"
exit /b %errorlevel%
