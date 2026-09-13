@echo off
setlocal
cd /d "%~dp0"

echo.
echo   === Telemost Music Bot : portable build ===
echo.

if not exist ".venv\Scripts\python.exe" goto nosetup

echo   [1/5] Installing pinned build dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto fail

echo   [2/5] Installing Chromium inside the application package...
set PLAYWRIGHT_BROWSERS_PATH=0
".venv\Scripts\python.exe" -m playwright install --no-shell chromium
if errorlevel 1 goto fail

echo   [3/5] Building portable application...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean TelemostMusicBot.spec
if errorlevel 1 goto fail

echo   [4/5] Copying instructions...
copy /y "PORTABLE_README.txt" "dist\TelemostMusicBot\PORTABLE_README.txt" >nul
copy /y "README.md" "dist\TelemostMusicBot\README.md" >nul

echo   [5/5] Creating ZIP archive...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\TelemostMusicBot' -DestinationPath 'dist\TelemostMusicBot-portable.zip' -Force"
if errorlevel 1 goto fail

echo.
echo   Portable build is ready:
echo   dist\TelemostMusicBot-portable.zip
echo.
pause
exit /b 0

:nosetup
echo   [ERROR] Run setup.bat first on the build computer.
echo.
pause
exit /b 1

:fail
echo.
echo   [ERROR] Portable build failed. Scroll up and copy the error text.
echo.
pause
exit /b 1
