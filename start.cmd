@echo off
cd /d "%~dp0"
set "PATH=%CD%\ffmpeg;%PATH%"
set "NO_PROXY=*"
"runtime\Python312\python.exe" app.py
if errorlevel 1 pause
