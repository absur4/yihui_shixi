@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv does not exist. Run setup_windows.bat first.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" launch.py run --config config.yaml --output outputs\results.json
set "BENCH_EXIT=%errorlevel%"
if not "%BENCH_EXIT%"=="0" pause
exit /b %BENCH_EXIT%

