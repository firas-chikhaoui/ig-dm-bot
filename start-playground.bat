@echo off
title ig-dm-bot playground
cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo  Python is not installed.
  echo  Install it from https://www.python.org/downloads/
  echo  IMPORTANT: tick "Add python.exe to PATH" during the install, then run this file again.
  echo.
  pause
  exit /b
)

if not exist ".venv\Scripts\activate.bat" (
  echo Creating the Python environment ^(first time only^)...
  python -m venv .venv
)
call ".venv\Scripts\activate.bat"

echo Installing packages ^(first time can take a minute^)...
python -m pip install -q --disable-pip-version-check -r requirements.txt
if errorlevel 1 ( echo Package install failed. & pause & exit /b )

set DRY_RUN=true
set DB_PATH=data\playground.db

echo.
echo  ============================================================
echo   Bot is starting. The playground opens in your browser.
echo   If not, open:  http://localhost:8000/playground
echo   KEEP THIS WINDOW OPEN. Close it to stop the bot.
echo  ============================================================
echo.
start "" cmd /c "timeout /t 4 >nul & start http://localhost:8000/playground"
python -m uvicorn app.main:app --port 8000
pause
