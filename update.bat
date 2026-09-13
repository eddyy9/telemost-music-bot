@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto nosetup

echo.
echo   Updating yt-dlp...
echo   (fixes most "cannot download" errors - YouTube breaks it monthly)
echo.

".venv\Scripts\python.exe" -m pip install --upgrade yt-dlp
if errorlevel 1 goto fail

echo.
echo   Done. Launch start.bat as usual.
echo.
pause
exit /b 0

:nosetup
echo.
echo   [ERROR] Run setup.bat first.
echo.
pause
exit /b 1

:fail
echo.
echo   [ERROR] Update failed. Check your internet connection.
echo.
pause
exit /b 1
