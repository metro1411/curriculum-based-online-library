@echo off
setlocal
cd /d "%~dp0"

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

call .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r requirements-dev.txt
if errorlevel 1 goto :error
call .venv\Scripts\python.exe -m pytest
pause
exit /b %errorlevel%

:error
echo Test setup could not complete. Check your internet connection and Python installation.
pause
exit /b 1
