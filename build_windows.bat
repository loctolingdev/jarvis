@echo off
cd /d "%~dp0"
title Build Jarvis.exe

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo Python n'est pas installe ou pas dans le PATH.
    pause
    exit /b 1
)

echo Mise a jour de pip...
python -m pip install --upgrade pip
if %errorlevel% neq 0 ( echo ERREUR pip & pause & exit /b 1 )

echo Installation des dependances...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 ( echo ERREUR dependances & pause & exit /b 1 )

echo Installation de PyInstaller...
python -m pip install pyinstaller
if %errorlevel% neq 0 ( echo ERREUR pyinstaller & pause & exit /b 1 )

echo Nettoyage des builds precedents...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo Compilation...
pyinstaller --noconfirm --clean Jarvis.spec
if %errorlevel% neq 0 (
    echo ERREUR de compilation PyInstaller.
    pause
    exit /b 1
)

if exist "dist\Jarvis.exe" (
    echo.
    echo ========================================
    echo BUILD OK : dist\Jarvis.exe
    echo ========================================
) else (
    echo BUILD FAILED : Jarvis.exe introuvable.
)
pause
exit /b 0
