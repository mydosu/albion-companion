@echo off
rem ASCII entry for the eye capture agent (silent launcher cannot start the exe directly).
rem Token is read from token.txt next to this file (not tracked; copy token.txt.example). No built-in fallback.
cd /d "%~dp0"
set "EYES_TOK="
if exist "%~dp0token.txt" for /f "usebackq delims=" %%A in ("%~dp0token.txt") do if not defined EYES_TOK set "EYES_TOK=%%A"
if not defined EYES_TOK (
  echo [eyes] missing or empty %~dp0token.txt - create it from token.txt.example, one line: the token 1>&2
  exit /b 1
)
"%~dp0..\..\..\tts\GPT-sovits\runtime\python.exe" -u capture_agent.py --port 8790 --token-file "%~dp0token.txt" --max-width 0 --bind 127.0.0.1
