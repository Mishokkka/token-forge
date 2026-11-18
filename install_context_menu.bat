@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if %errorlevel% neq 0 (
    echo ERROR: Could not enter the program directory.
    pause
    exit /b 1
)
title TokenForge - Context menu setup
if not exist ".venv\Scripts\python.exe" goto missing
".venv\Scripts\python.exe" "context_menu.py" --install
if %errorlevel% neq 0 goto failed
echo.
echo Done. Right-click an image in Explorer.
echo Windows 11: choose Show more options if needed.
pause
exit /b 0

:missing
echo ERROR: Run install.bat before installing the context menu.
goto failed
:failed
echo ERROR: Could not install context menu.
pause
exit /b 1
