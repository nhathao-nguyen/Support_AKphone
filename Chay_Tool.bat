@echo off
setlocal
cd /d "%~dp0"
echo Starting AKPhone Smart Dispatcher...
python main.py
if errorlevel 1 pause
