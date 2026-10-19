@echo off
setlocal
cd /d "%~dp0"

echo ========================================================
echo  BUILDING AKPHONE DISPATCHER EXECUTABLE
echo ========================================================

if not exist "build" mkdir "build"
if not exist "dist" mkdir "dist"

python -m PyInstaller --noconfirm --distpath dist --workpath build AKPhone_Dispatcher_Recovered.spec
if errorlevel 1 (
    echo.
    echo Build failed with error code: %errorlevel%
    pause
    exit /b %errorlevel%
)

echo Synchronizing templates and configurations to dist...
if exist "app_settings.json" copy /y "app_settings.json" "dist\" >nul
if exist "template_script.txt" copy /y "template_script.txt" "dist\" >nul
if exist "template_config.ini" copy /y "template_config.ini" "dist\" >nul
if exist "template_cmt.txt" copy /y "template_cmt.txt" "dist\" >nul
if exist "docs\HUONG_DAN_VAN_HANH.txt" copy /y "docs\HUONG_DAN_VAN_HANH.txt" "dist\" >nul

if exist "configs" (
    if not exist "dist\configs" mkdir "dist\configs"
    copy /y "configs\*" "dist\configs\" >nul
)

if exist "assets" (
    if not exist "dist\assets" mkdir "dist\assets"
    copy /y "assets\*" "dist\assets\" >nul
)

echo ========================================================
echo  BUILD SUCCESSFUL!
echo  Executable: dist\AKPhone_Dispatcher_Recovered.exe
echo ========================================================
exit /b 0
