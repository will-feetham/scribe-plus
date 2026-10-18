@echo off
REM Starts Scribe Plus in the background (no console window).
cd /d "%~dp0"
if not exist "venv\Scripts\pythonw.exe" (
    echo Scribe Plus is not set up yet. Run "Install Scribe Plus.bat" first.
    pause
    exit /b 1
)
start "" "venv\Scripts\pythonw.exe" "scribe_plus_windows.py"
