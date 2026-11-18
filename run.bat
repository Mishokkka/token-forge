@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 goto bad_directory
if not errorlevel 0 goto bad_directory
title TokenForge
if not exist ".venv\Scripts\python.exe" goto need_install
".venv\Scripts\python.exe" "tokenforge.py" %*
if errorlevel 1 goto failed
if not errorlevel 0 goto failed
exit /b 0

:need_install
echo ERROR: Python environment missing. Run install.bat first.
pause
exit /b 1

:failed
echo.
echo ERROR: TokenForge stopped. See error details above.
pause
exit /b 1

:bad_directory
echo ERROR: Could not enter the program directory.
pause
exit /b 1
