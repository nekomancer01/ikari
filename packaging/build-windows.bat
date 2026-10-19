@echo off
rem Double-click to build SubredditSentiment.exe (see build-windows.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-windows.ps1"
pause
