@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python n'est pas installe ou pas dans le PATH.
    pause
    exit /b 1
)
if not exist ".env" (
    if exist ".env.example" copy ".env.example" ".env" >nul
)
python enroll_voice.py
if %errorlevel% neq 0 (
    echo Erreur lors de l'enregistrement vocal.
    pause
)
exit /b 0
