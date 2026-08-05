@echo off
cd /d "%~dp0"

if "%1"=="serve" (
    echo [Memory Hub] Starting server...
    for /f "tokens=*" %%p in ('where python') do set "HUB_PYTHON=%%p"
    if defined HUB_PYTHON (start "Memory Hub" %HUB_PYTHON% hub.py serve 8921) else (echo Python not found && pause && exit /b 1)
    timeout /t 1 /nobreak >nul
    echo [Memory Hub] Ready at http://127.0.0.1:8921
) else (
    python hub.py %*
)
