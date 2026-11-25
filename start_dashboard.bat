@echo off
title NIFTY 50 Groww AI Bolt Dashboard
cd /d "%~dp0"

echo ============================================================
echo   NIFTY 50 AI BOLT DASHBOARD - GROWW OPTION 1
echo   Initializing web dashboard at http://127.0.0.1:5050 ...
echo ============================================================
echo.

:: Free port 5050 if an older process is still holding it
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :5050') do taskkill /f /pid %%a >nul 2>&1

:: Launch the browser after 1.5 seconds in background
start "" cmd /c "timeout /t 2 /nobreak >nul && start http://127.0.0.1:5050"

:: Start the upgraded Python web server
python web_server.py
pause
