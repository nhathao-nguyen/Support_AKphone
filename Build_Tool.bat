@echo off
setlocal
cd /d "%~dp0"
set "PYINSTALLER_CONFIG_DIR=%~dp0..\tools\pyinstaller-cache"
set "TEMP=%~dp0..\tools\tmp"
set "TMP=%TEMP%"
if not exist "%TEMP%" mkdir "%TEMP%"
python -m PyInstaller --noconfirm --distpath dist --workpath build AKPhone_Dispatcher_Recovered.spec
if errorlevel 1 exit /b %errorlevel%
copy /y app_settings.json dist\app_settings.json >nul
copy /y template_script.txt dist\template_script.txt >nul
copy /y template_config.ini dist\template_config.ini >nul
exit /b %errorlevel%
