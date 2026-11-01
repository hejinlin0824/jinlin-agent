@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\test-all.ps1"
set "RESULT=%ERRORLEVEL%"
echo.
if "%RESULT%"=="0" (
  echo Jinlin Agent validation: ALL PASSED
) else (
  echo Jinlin Agent validation: FAILED with exit code %RESULT%
)
pause
exit /b %RESULT%
