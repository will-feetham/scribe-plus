@echo off
REM Replays your saved dictations through every engine you have keys for.
REM Extra options pass through, for example:  Benchmark.bat --clips 30
cd /d "%~dp0"
"venv\Scripts\python.exe" benchmark.py %*
