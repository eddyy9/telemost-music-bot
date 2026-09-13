@echo off
cd /d "%~dp0"

echo.
echo   === Telemost Music Bot : setup ===
echo.

if not exist "requirements.txt" goto nofiles

where python >nul 2>&1
if errorlevel 1 goto nopython

echo   [1/3] Creating virtual environment...
if not exist ".venv\Scripts\python.exe" python -m venv .venv
if not exist ".venv\Scripts\python.exe" goto novenv

echo   [2/3] Installing dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto fail

echo   [3/3] Installing browser for Playwright...
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto fail

echo.
echo   Setup complete.
echo.
echo   Next: install VB-Audio Virtual Cable (vb-audio.com/Cable),
echo   run the installer as Administrator, then reboot.
echo   After that just launch start.bat
echo.
pause
exit /b 0

:nofiles
echo   [ERROR] requirements.txt not found next to this file.
echo   Keep setup.bat, start.bat, bot.py, audio.py, browser.py
echo   and requirements.txt together in one folder.
echo.
pause
exit /b 1

:nopython
echo   [ERROR] Python not found in PATH.
echo   Install Python 3.10+ from python.org and enable
echo   the checkbox "Add python.exe to PATH" during install.
echo.
pause
exit /b 1

:novenv
echo   [ERROR] Could not create the .venv folder.
echo   Try deleting the .venv folder and running setup.bat again.
echo.
pause
exit /b 1

:fail
echo.
echo   [ERROR] Install step failed. Scroll up and copy the error text.
echo.
pause
exit /b 1
