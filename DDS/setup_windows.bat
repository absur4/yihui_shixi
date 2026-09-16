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

rem fastddsgen needs the MSVC preprocessor (cl.exe). When cl.exe is not on
rem PATH, enter the VS developer environment automatically.
where cl.exe >nul 2>nul
if not errorlevel 1 goto :build

set "VSPATH="
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" goto :try_default
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSPATH=%%i"

:try_default
if defined VSPATH goto :have_vs
if not exist "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvars64.bat" goto :no_msvc
set "VSPATH=C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools"

:have_vs
echo Initializing MSVC environment: "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat"
call "%VSPATH%\VC\Auxiliary\Build\vcvars64.bat" >nul
goto :build

:no_msvc
echo WARNING: MSVC environment not found; fastddsgen may fail.
echo          Run this script from "x64 Native Tools Command Prompt for VS".

:build
echo [3/4] Building Fast DDS Python binding and IDL type support...
rem %* must be forwarded. Without it, "setup_windows.bat --force" loses the
rem --force flag, the script skips the binding rebuild, and the declared
rem version no longer matches the installed one.
".venv\Scripts\python.exe" tools\setup_windows.py %*
if errorlevel 1 goto :failed

echo [4/4] Setup complete. You can now run run_one_example.bat.
exit /b 0

:failed
echo.
echo SETUP FAILED. Read the error above and README.md section 3.
pause
exit /b 1
