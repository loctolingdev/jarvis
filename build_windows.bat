@echo off
cd /d "%~dp0"
title Build Jarvis.exe
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install pyinstaller
pyinstaller --noconfirm --clean Jarvis.spec
if exist "dist\Jarvis.exe" (
  echo BUILD OK: dist\Jarvis.exe
) else (
  echo BUILD FAILED
)
pause
