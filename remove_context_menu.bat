@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TokenForge - Remove context menu
if exist ".venv\Scripts\python.exe" goto run_venv
where py >nul 2>&1
if errorlevel 1 goto missing
py -3.12 "context_menu.py" --remove
if errorlevel 1 goto failed
goto success

:run_venv
".venv\Scripts\python.exe" "context_menu.py" --remove
if errorlevel 1 goto failed
goto success

:missing
echo ERROR: Python missing. Install Python or use regedit to remove entries.
goto failed
:success
echo Context menu entries removed.
pause
exit /b 0
:failed
echo ERROR: Could not remove context menu entries.
pause
exit /b 1
