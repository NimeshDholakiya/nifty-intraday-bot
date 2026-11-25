@echo off
title NIFTY 50 Intraday Signal Bot (Groww / Angel One)
cd /d "%~dp0"
echo ============================================================
echo   NIFTY 50 Intraday Bot - Live AI Bolt Signal Mode (Groww)
echo ============================================================
echo.
if not exist ".env" (
    echo [ERROR] .env file not found!
    echo Please copy .env.example to .env and configure your settings.
    echo.
    pause
    exit /b 1
)
python main.py --mode live
echo.
pause
