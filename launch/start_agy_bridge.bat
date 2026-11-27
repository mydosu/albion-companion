@echo off
rem agy bridge for the Albion brain (design: docs/任务书「阿尔比恩agy内核」A 节).
rem Listens on 127.0.0.1:8670, owns `agy acp` children. Started hidden by
rem launch/start_all.bat through silent_run.ps1; logs go to logs\agy-bridge.log.
setlocal
for %%I in ("%~dp0..") do set "WIFE=%%~fI"
set "SYS_PY=python"
if defined ALBION_PYTHON set "SYS_PY=%ALBION_PYTHON%"
if not exist "%WIFE%\logs" mkdir "%WIFE%\logs"
cd /d "%WIFE%"
"%SYS_PY%" "%WIFE%\brain\agy\bridge.py"
endlocal
