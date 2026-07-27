@echo off
setlocal
cd /d "%~dp0"

if not exist ".env" (
  copy /Y ".env.example" ".env" >nul
  echo A new .env file has been created.
) else (
  echo Opening the existing .env file. Your existing values will not be replaced.
)

echo.
echo In .env, set HOD_ACTIVATION_CODE to a private value of at least 16 characters.
echo Add GEMINI_API_KEY only when you have a real Google AI Studio key.
echo Leave GEMINI_API_KEY empty if you want to run without generated AI answers.
echo.
notepad .env
echo.
echo Configuration window closed. You can now choose Start locally from START_HERE.bat.
pause
