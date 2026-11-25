@echo off
rem Double-click to start the video downloader.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo First run: setting up... this takes a minute.
    py -3 -m venv .venv || python -m venv .venv || goto :nopython
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :failed
)

rem Keep yt-dlp current - YouTube changes often and old versions stop working.
".venv\Scripts\python.exe" -m pip install -q --upgrade "yt-dlp[default]" >nul 2>&1

".venv\Scripts\python.exe" app.py
pause
exit /b

:nopython
echo Python 3.10+ is required. Install it from https://www.python.org/downloads/ and try again.
pause
exit /b 1

:failed
echo Installing the requirements failed. Check your internet connection and try again.
pause
exit /b 1
