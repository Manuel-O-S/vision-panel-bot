@echo off
title VISION BOT
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] No se encontro el entorno virtual .venv
    echo Crealo con:  python -m venv .venv
    echo y luego:     .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

".venv\Scripts\python.exe" main.py
if errorlevel 1 (
    echo.
    echo [ERROR] VISION BOT se cerro con un error. Revisa el mensaje de arriba
    echo o el archivo logs\vision_bot.log
    pause
)
