@echo off
REM Optional: offline speech-to-text with faster-whisper on the CPU.
REM On a laptop without an NVIDIA GPU, expect a few seconds per dictation.
cd /d "%~dp0"
echo Installing faster-whisper for offline transcription ...
"venv\Scripts\python.exe" -m pip install -r requirements-local.txt --disable-pip-version-check
if errorlevel 1 goto :fail
echo Downloading the base.en model once, about 150 MB ...
"venv\Scripts\python.exe" -c "from faster_whisper import WhisperModel; WhisperModel('base.en', device='cpu', compute_type='int8'); print('Model ready.')"
if errorlevel 1 goto :fail
echo.
echo Done. In the tray menu pick Transcription ^> Local Whisper on this PC.
pause
exit /b 0

:fail
echo Install failed; see the error above.
pause
exit /b 1
