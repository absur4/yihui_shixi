@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" exit /b 2
".venv\Scripts\python.exe" tools\verify_example.py --input results\one_example
exit /b %errorlevel%
