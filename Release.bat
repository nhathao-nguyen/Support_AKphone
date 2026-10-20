@echo off
setlocal
cd /d "%~dp0"

rem Release a new version: Release.bat 2.1.0
rem Sets __version__, commits, tags v2.1.0 and pushes. GitHub Actions then builds the exe,
rem publishes the release, and every running tool offers the update to its user.

if "%~1"=="" (
    echo Cach dung: Release.bat 2.1.0
    exit /b 1
)
set "VER=%~1"
if /i "%VER:~0,1%"=="v" set "VER=%VER:~1%"

set "PY=python"
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

git diff --quiet && git diff --cached --quiet
if errorlevel 1 (
    echo Con thay doi chua commit. Hay commit truoc roi chay lai.
    exit /b 1
)

"%PY%" scripts\set_version.py %VER% || exit /b 1
git diff --quiet || git commit -q -am "release: v%VER%" || exit /b 1
git tag -a "v%VER%" -m "v%VER%" || exit /b 1
git push origin HEAD || exit /b 1
git push origin "v%VER%" || exit /b 1

echo.
echo Da day v%VER%. Theo doi build tai: https://github.com/nhathao-nguyen/Support_AKphone/actions
exit /b 0
