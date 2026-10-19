@echo off
REM Scribe Plus setup for Windows. Also the updater: run a newer copy and it
REM replaces the installed one, keeping your API keys, settings and history.
REM   0. a downloaded copy installs itself to %LOCALAPPDATA%\Programs\Scribe Plus
REM      (a git checkout, like a developer's, runs where it is)
REM   1. finds Python 3.10-3.13 (offers to install 3.12 with winget)
REM   2. creates venv\ and installs requirements-plus.txt
REM   3. asks for your Groq API key (saved to .env, which git ignores)
REM   4. Start menu entry, and optionally start Scribe Plus when you sign in
REM   5. launches it
setlocal EnableDelayedExpansion
cd /d "%~dp0"
title Scribe Plus setup

echo ============================================================
echo   Scribe Plus setup
echo ============================================================
echo.

REM --- 0. Install location ------------------------------------------------------
set "TARGET=%LOCALAPPDATA%\Programs\Scribe Plus"
if exist "%~dp0.git" goto :here
if /i "%~dp0"=="%TARGET%\" goto :here
echo Installing to %TARGET% ...
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -like 'python*' -and $_.CommandLine -like '*scribe_plus_windows.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
if not exist "%TARGET%" mkdir "%TARGET%"
robocopy "%~dp0." "%TARGET%" /MIR /XD venv .git __pycache__ /XF .env /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto :fail
if exist "%~dp0.env" if not exist "%TARGET%\.env" copy /y "%~dp0.env" "%TARGET%\.env" >nul
endlocal & call "%LOCALAPPDATA%\Programs\Scribe Plus\Install Scribe Plus.bat"
exit /b %errorlevel%
:here

REM --- 1. Python -------------------------------------------------------------
set "PYEXE="
for %%V in (3.12 3.13 3.11 3.10) do (
    if not defined PYEXE (
        for /f "delims=" %%P in ('py -%%V -c "import sys;print(sys.executable)" 2^>nul') do set "PYEXE=%%P"
    )
)
if not defined PYEXE (
    for /f "delims=" %%P in ('python -c "import sys;print(sys.executable if (3,10)<=sys.version_info[:2]<=(3,13) else str())" 2^>nul') do set "PYEXE=%%P"
)
if not defined PYEXE (
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PYEXE (
    echo Python 3.10 to 3.13 was not found on this PC.
    choice /c YN /m "Install Python 3.12 now with winget"
    if errorlevel 2 goto :nopython
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PYEXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not defined PYEXE goto :nopython
echo Using Python: %PYEXE%

REM --- 2. venv + dependencies ----------------------------------------------------
if not exist "venv\Scripts\python.exe" (
    echo Creating a private Python environment in venv ...
    "%PYEXE%" -m venv venv
    if errorlevel 1 goto :fail
)
echo Installing dependencies, about a minute the first time ...
"venv\Scripts\python.exe" -m pip install --upgrade pip --quiet --disable-pip-version-check
"venv\Scripts\python.exe" -m pip install -r requirements-plus.txt --disable-pip-version-check
if errorlevel 1 goto :fail

REM --- 3. Groq API key -----------------------------------------------------------
set "NEED_KEY=1"
if exist ".env" (
    findstr /b /c:"GROQ_API_KEY=" ".env" >nul 2>nul
    if not errorlevel 1 set "NEED_KEY=0"
)
if "%NEED_KEY%"=="1" (
    echo.
    echo Scribe Plus uses Groq for speech-to-text and cleanup. The free tier covers personal use.
    echo Get a key at https://console.groq.com/keys : sign in, Create API Key, copy it.
    set /p "GROQ_KEY=Paste your Groq API key and press Enter, or just press Enter to add it later: "
    if not "!GROQ_KEY!"=="" (
        >>".env" echo GROQ_API_KEY=!GROQ_KEY!
        echo Saved to .env, which git ignores.
    )
)

REM --- 4. Start menu, and start at sign-in? ---------------------------------------
REM The same code Scribe Plus runs each time it starts (scribe_plus\win\shell.py):
REM a Start menu entry with its icon, and notifications that say "Scribe Plus".
set "STARTUP_LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Scribe Plus.lnk"
set "SHELL_ARGS="
if exist "%STARTUP_LNK%" goto :shortcuts
echo.
choice /c YN /m "Start Scribe Plus automatically when you sign in to Windows"
if not errorlevel 2 set "SHELL_ARGS=--startup"
:shortcuts
"venv\Scripts\python.exe" -m scribe_plus.win.shell %SHELL_ARGS%
if errorlevel 1 echo Couldn't add the Start menu entry now; Scribe Plus adds it itself when it starts.
if exist "%STARTUP_LNK%" echo Starts when you sign in. Delete "Scribe Plus" from your Startup folder to undo.

:launch
echo.
echo Starting Scribe Plus. Look for the Scribe Plus icon (five bars) in the system tray
echo (click the ^^ arrow by the clock if it is hidden).
start "" "venv\Scripts\pythonw.exe" "scribe_plus_windows.py"
echo.
echo   Hold Ctrl+Win and talk, release to paste.
echo   Hold Ctrl+Win+Alt to rewrite selected text by voice.
echo   Double-tap Ctrl+Win for hands-free; press it again to finish. Esc cancels.
echo.
pause
exit /b 0

:nopython
echo.
echo Install Python 3.12 from https://www.python.org/downloads/
echo and tick "Add python.exe to PATH", then run this file again.
pause
exit /b 1

:fail
echo.
echo Setup failed. Scroll up for the error message.
pause
exit /b 1
