@echo off
chcp 65001 >nul
cd /d "%~dp0"
python generator_gui.py
if errorlevel 1 pause
