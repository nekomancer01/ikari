@echo off
rem Double-click to run ikari from source on Windows.
rem First run creates a private Python environment in .venv (takes a minute).
rem Delete the .venv folder to reinstall from scratch.
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" goto :deps

where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
echo First run: setting up ikari...
%PY% -m venv .venv
if errorlevel 1 goto :nopython
".venv\Scripts\python.exe" -m pip install --upgrade pip

:deps
rem Re-sync dependencies whenever pyproject.toml changes (new deps never break a launch).
if exist ".venv\.deps-stamp" (
    for /f %%i in ('powershell -nologo -command "if ((Get-Item pyproject.toml).LastWriteTime -gt (Get-Item .venv\.deps-stamp).LastWriteTime) {1} else {0}"') do if "%%i"=="0" goto :run
)
echo Installing ikari dependencies...
".venv\Scripts\python.exe" -m pip install -q -e .
if errorlevel 1 goto :fail
type nul > ".venv\.deps-stamp"

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
