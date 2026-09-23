@echo off
cd /d "%~dp0.."
echo === Channel Rewriter setup ===

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv || (echo Python not found. Install Python 3.10+ and tick "Add to PATH". & pause & exit /b 1)
)
echo Installing packages...
venv\Scripts\python -m pip install --upgrade pip >nul
venv\Scripts\python -m pip install -r requirements.txt || (pause & exit /b 1)

if not exist .env (
    copy .env.example .env >nul
    echo.
    echo Created .env - fill it in now, then press any key to continue.
    notepad .env
    pause
)

if not exist reader.session (
    echo.
    echo === Logging in the reader account ===
    venv\Scripts\python login.py
)
echo.
echo Done. Start the bot with run.bat, or run_tray.bat for tray mode.
pause
