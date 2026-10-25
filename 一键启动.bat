@echo off
setlocal
rem 兼容原入口：默认 dev、显示各服务控制台；exe/prod 参数仍可用。
set ALBION_SHOW_CONSOLES=1
if "%~1"=="" (
  call "%~dp0start_all.bat" dev
) else (
  call "%~dp0start_all.bat" %*
)
exit /b %errorlevel%
