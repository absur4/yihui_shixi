@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv does not exist. Run setup_windows.bat first.
  pause
  exit /b 2
)
".venv\Scripts\python.exe" launch.py run --config config.example.yaml --output results\one_example --overwrite
set "BENCH_EXIT=%errorlevel%"
if not "%BENCH_EXIT%"=="0" (
  echo.
  echo EXAMPLE FAILED with exit code %BENCH_EXIT%. Check logs under results\one_example\artifacts.
  pause
)
exit /b %BENCH_EXIT%
