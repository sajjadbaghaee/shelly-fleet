@echo off
title Shelly Fleet - Install dependencies
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Installation failed. Check that Python is installed and on PATH.
  pause
  exit /b 1
)
echo.
echo Dependencies installed. Launch start_shelly_fleet.bat
pause
