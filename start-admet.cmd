@echo off
setlocal
cd /d "%~dp0"
uv run --locked --extra control admet qt %*
if errorlevel 1 (
    echo ADMET could not start or reported a shutdown error. Review the message above.
    pause
    exit /b 1
)
