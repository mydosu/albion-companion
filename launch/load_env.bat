@echo off
rem launch/load_env.bat - load repo-root config/local.env into this process environment.
rem
rem Rules:
rem   * one KEY=VALUE per line; blank lines and lines starting with # are skipped
rem   * a variable that is already defined is NOT overridden (if not defined KEY set ...),
rem     so a real environment variable / an explicit "set" before launching always wins
rem   * values are taken literally: no quoting, no %VAR% expansion, no 0x/hex parsing
rem   * missing file = silent success (fresh clone, or no local overrides at all)
rem
rem Call this BEFORE the caller runs `setlocal enabledelayedexpansion`: with delayed
rem expansion on, a `!` inside a value would be eaten while this file expands it.
set "ALBION_LOCAL_ENV=%~dp0..\config\local.env"
if not exist "%ALBION_LOCAL_ENV%" exit /b 0
for /f "usebackq eol=# tokens=1,* delims==" %%A in ("%ALBION_LOCAL_ENV%") do (
    if not "%%~A"=="" if not defined %%~A set "%%~A=%%~B"
)
exit /b 0
