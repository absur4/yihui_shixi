@echo off
setlocal
cd /d "%~dp0"

if not defined FASTDDSHOME (
  echo ERROR: FASTDDSHOME is not set. Example: set FASTDDSHOME=D:\fastdds
  pause
  exit /b 2
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating Python 3.11 virtual environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :failed
)

echo [2/4] Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo [3/4] Building Fast DDS Python binding and IDL type support...
".venv\Scripts\python.exe" tools\setup_windows.py
if errorlevel 1 goto :failed

echo [4/4] Setup complete. You can now run run_one_example.bat.
exit /b 0

:failed
echo.
echo SETUP FAILED. Read the error above and README.md section 3.
pause
exit /b 1

