@echo off
setlocal
cd /d "%~dp0"
vsoa.exe --config config.yaml --suite qualification --output outputs\qualification %*
set "RESULT_CODE=%ERRORLEVEL%"
if /i not "%VSOA_NO_PAUSE%"=="1" pause
exit /b %RESULT_CODE%
