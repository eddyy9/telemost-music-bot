@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" goto nosetup

set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
".venv\Scripts\python.exe" diag.py
exit /b 0

:nosetup
echo.
echo   [ERROR] Run setup.bat first.
echo.
pause
exit /b 1
