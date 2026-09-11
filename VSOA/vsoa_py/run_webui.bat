@echo off
setlocal
cd /d "%~dp0"
set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

echo [VSOA WebUI] Using: %PY%
"%PY%" webui\server.py --open
if errorlevel 1 (
  echo.
  echo WebUI failed to start. Check that Python is available and run this file from the VSOA project root.
  pause
)
endlocal
