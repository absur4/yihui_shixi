@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" exit /b 2
".venv\Scripts\python.exe" tools\verify_example.py --input outputs\one_example.json
exit /b %errorlevel%
