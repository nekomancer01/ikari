@echo off
rem Double-click to pull the latest code, build the Windows app and start it (for testing).
rem Options (pass on the command line): -Test  -NoPull  -NoLaunch  -Branch <name>
rem Uses a separate build environment in .venv-build; output lands in dist\.
setlocal
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "packaging\dev-windows.ps1" %*
if errorlevel 1 (
    echo.
    echo Failed. See the messages above.
    pause
    exit /b 1
)
exit /b 0
