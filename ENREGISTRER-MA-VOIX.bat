@echo off
cd /d "%~dp0"
py enroll_voice.py
if errorlevel 1 python enroll_voice.py
