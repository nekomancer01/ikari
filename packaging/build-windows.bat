@echo off
rem Double-click to build the Windows app, zip and installer into dist\ (see build.py).
rem Uses a separate build environment in .venv-build.
setlocal
cd /d "%~dp0.."
where py >nul 2>nul && (set "PY=py -3") || (set "PY=python")
if not exist ".venv-build\Scripts\python.exe" %PY% -m venv .venv-build
if errorlevel 1 goto :fail
".venv-build\Scripts\python.exe" -m pip install --upgrade pip
".venv-build\Scripts\python.exe" -m pip install . pyinstaller
if errorlevel 1 goto :fail
".venv-build\Scripts\python.exe" packaging\build.py
if errorlevel 1 goto :fail
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
