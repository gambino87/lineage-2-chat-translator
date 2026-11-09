@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start Translator.ps1"
if errorlevel 1 (
    echo.
    echo The translator could not start. Review the message above.
    pause
)
endlocal
