@echo off
chcp 65001 >nul
rem Denia backend (upstream GPT-SoVITS api_v2) :9880 [CPU resident].
rem GSV install dir: ALBION_GSV_ROOT from config\local.env, default <repo>\tts\GPT-sovits (gitignored).
rem Upstream code/weights are not in this repo - see tts\gsv\README.md.
call "%~dp0..\..\launch\load_env.bat"
if not defined ALBION_GSV_ROOT (
  for %%I in ("%~dp0..\..\tts\GPT-sovits") do set "ALBION_GSV_ROOT=%%~fI"
)
if not exist "%ALBION_GSV_ROOT%\runtime\python.exe" (
  echo [denia] GSV runtime not found: "%ALBION_GSV_ROOT%\runtime\python.exe"
  echo [denia] point ALBION_GSV_ROOT at a GPT-SoVITS install - see tts\gsv\README.md
  if not defined ALBION_SILENT pause
  exit /b 1
)
if not defined ALBION_DENIA_CONFIG set "ALBION_DENIA_CONFIG=%~dp0tts_infer_denia.yaml"
if not defined ALBION_DENIA_PORT set "ALBION_DENIA_PORT=9880"
echo ============================================
echo  Denia backend (GPT-SoVITS api_v2)  :%ALBION_DENIA_PORT%  [CPU]
echo  GSV root : %ALBION_GSV_ROOT%
echo  config   : %ALBION_DENIA_CONFIG%
echo  first load is slow; start the bridge after the port listens
echo ============================================
echo.
"%ALBION_GSV_ROOT%\runtime\python.exe" "%~dp0denia_launch.py" -c "%ALBION_DENIA_CONFIG%" -a 127.0.0.1 -p %ALBION_DENIA_PORT%
if not defined ALBION_SILENT pause
