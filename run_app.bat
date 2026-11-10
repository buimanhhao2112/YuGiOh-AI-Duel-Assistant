@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    where uv >nul 2>nul
    if %errorlevel%==0 (
        uv venv --python 3 .venv
    ) else (
        py -3 -m venv .venv
    )
)

if not exist ".venv\Scripts\python.exe" (
    echo Unable to create the Python environment. Install Python 3.11 or newer and try again.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" run_app.py %*
if errorlevel 1 (
    echo.
    echo Startup failed. See the error above.
    pause
)
endlocal
