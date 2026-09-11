@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0vsoa.exe" --config "%~dp0config.yaml" %*
set "RESULT_CODE=%ERRORLEVEL%"
echo.
echo Exit code: %RESULT_CODE%
echo Default result: %~dp0outputs\result.json
echo If overridden, use the RESULT path printed above.
if /i not "%VSOA_NO_PAUSE%"=="1" pause
exit /b %RESULT_CODE%
