@echo off
setlocal
cd /d "%~dp0"

:menu
cls
echo ==============================================================
echo   SMART DIT LEARNING HUB - START HERE
echo ==============================================================
echo.
echo  1. Configure local settings (.env)
echo  2. Test the complete application
echo  3. Start the website locally
echo  4. Open the Render deployment guide
echo  5. Exit
echo.
choice /C 12345 /N /M "Choose a number"
if errorlevel 5 goto end
if errorlevel 4 goto deploy
if errorlevel 3 goto run
if errorlevel 2 goto test
if errorlevel 1 goto configure

:configure
call configure_local.bat
goto menu

:test
call test_app.bat
goto menu

:run
call run_local.bat
goto menu

:deploy
notepad "DEPLOY_RENDER.md"
goto menu

:end
endlocal
