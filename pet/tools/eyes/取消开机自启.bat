@echo off
chcp 936 >nul
set "F=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\阿尔比恩的眼睛.vbs"
if exist "%F%" (del "%F%" && echo 已取消开机自启。) else (echo 本来就没装。)
pause
