@echo off
cd /d "%~dp0"
echo Starting the app... Browser will open shortly.
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python 3 not found in PATH.
  echo Please install Python 3 and check "Add Python to PATH" during setup.
  echo.
  pause
  exit /b 1
)

python server.py
echo.
echo Server stopped. Press any key to close.
pause
