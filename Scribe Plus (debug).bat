@echo off
REM Runs Scribe Plus with its log printed here. Quit the normal copy first
REM (tray menu > Quit), since only one copy can run at a time.
cd /d "%~dp0"
echo Scribe Plus with a live log. Close this window or use the tray menu to quit.
echo.
"venv\Scripts\python.exe" scribe_plus_windows.py --debug
pause
