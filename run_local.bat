@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
  echo.
  echo Creating your local .env configuration file...
  copy /Y ".env.example" ".env" >nul
  echo Configure the optional Gemini key and required HOD activation code now.
  call configure_local.bat
)

where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.12 or later is required. Install it from https://www.python.org/downloads/
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  py -3 -m venv .venv
  if errorlevel 1 goto :error
)

call .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Smart DIT Learning Hub is starting at http://127.0.0.1:5055
echo Use Ctrl+C in this window to stop the website.
call .venv\Scripts\python.exe app.py
exit /b %errorlevel%

:error
echo Setup could not complete. Check your internet connection and Python installation.
pause
exit /b 1
