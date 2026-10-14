@echo off
cd /d "%~dp0"
title Jarvis Voice - loctoling
where py >nul 2>nul
if %errorlevel%==0 (
  py -m pip install -r requirements.txt
  py jarvis.py
) else (
  python -m pip install -r requirements.txt
  python jarvis.py
)
exit
