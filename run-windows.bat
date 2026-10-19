@echo off
rem Double-click to run Subreddit Sentiment from source on Windows.
rem First run creates a private Python environment in .venv (takes a minute).
rem Delete the .venv folder to reinstall from scratch.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto :run

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
echo First run: setting up Subreddit Sentiment...
%PY% -m venv .venv
if errorlevel 1 goto :nopython
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 goto :fail

:run
start "" ".venv\Scripts\pythonw.exe" -m subsentiment
exit /b 0

:nopython
echo.
echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/
echo (tick "Add python.exe to PATH" in the installer), then run this file again.
if exist .venv rmdir /s /q .venv
pause
exit /b 1

:fail
echo.
echo Setup failed. See the messages above.
pause
exit /b 1
