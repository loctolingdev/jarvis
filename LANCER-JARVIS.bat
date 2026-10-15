@echo off
cd /d "%~dp0"
title Jarvis Voice - loctoling

REM Check Python
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python n'est pas installe ou pas dans le PATH.
    echo Telechargez Python 3.11 depuis https://www.python.org/downloads/
    echo Cochez "Add Python to PATH" lors de l'installation.
    pause
    exit /b 1
)

REM Create .env if missing
if not exist ".env" (
    if exist ".env.example" (
        copy ".env.example" ".env" >nul
        echo Fichier .env cree depuis .env.example. Editez-le avec vos cles.
    ) else (
        echo ATTENTION: .env manquant. Creez-le avec vos cles ElevenLabs/OpenRouter.
    )
)

REM Install dependencies
echo Installation des dependances...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo ERREUR: installation des dependances echouee.
    pause
    exit /b 1
)

REM Launch
echo Lancement de Jarvis...
python jarvis.py
if %errorlevel% neq 0 (
    echo Jarvis s'est arrete avec une erreur.
    pause
)
exit /b 0
