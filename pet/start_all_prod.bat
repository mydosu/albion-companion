@echo off
rem prod 与 exe 同义，缺 exe 时由唯一实现回退 dev。
call "%~dp0..\start_all.bat" prod
exit /b %errorlevel%
