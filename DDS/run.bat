@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv does not exist. Run setup_windows.bat first.
  pause
  exit /b 2
)
rem 产物布局：results\formal\result.json + results\formal\runs\<run_id>.json + results\formal\artifacts\<run_id>\
".venv\Scripts\python.exe" launch.py run --config config.yaml --output results\formal
set "BENCH_EXIT=%errorlevel%"
if not "%BENCH_EXIT%"=="0" pause
exit /b %BENCH_EXIT%
