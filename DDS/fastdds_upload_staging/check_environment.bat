@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: .venv does not exist. Run setup_windows.bat first.
  exit /b 2
)
".venv\Scripts\python.exe" launch.py check --config config.example.yaml
exit /b %errorlevel%

