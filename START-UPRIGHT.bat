@echo off
setlocal
cd /d "%~dp0"
title Upright - Local Website

echo =====================================
echo          UPRIGHT WEB APP
echo =====================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
  set "PYTHON=py"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/windows/
    echo During setup, tick "Add python.exe to PATH", then reopen this file.
    pause
    exit /b 1
  )
  set "PYTHON=python"
)

where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo FFmpeg was not found. Install FFmpeg and add its bin folder to PATH.
  echo Download: https://ffmpeg.org/download.html
  echo Then close and reopen this file.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a Python environment...
  %PYTHON% -m venv .venv
  if errorlevel 1 goto failed
)

echo Installing or updating required packages...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

echo.
echo Starting Upright. Keep this window open while using the website.
echo Open http://localhost:8000 in your browser.
echo Press Ctrl+C in this window to stop the app.
echo.
start "" http://localhost:8000
".venv\Scripts\python.exe" app.py
goto end

:failed
echo.
echo Setup failed. Check the error above and try again.
pause
exit /b 1

:end
pause
