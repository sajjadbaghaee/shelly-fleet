@echo off
rem Launch Shelly Fleet using the user's persistent local configuration.
cd /d "%~dp0"
python shelly_fleet_gui.py
if errorlevel 1 (
  echo.
  echo Shelly Fleet could not start. Run install_dependencies.bat and try again.
  pause
)
