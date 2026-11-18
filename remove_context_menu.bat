@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if %errorlevel% neq 0 (
    echo ERROR: Could not enter the program directory.
    pause
    exit /b 1
)
title TokenForge - Remove context menu
if not exist ".venv\Scripts\python.exe" goto try_launcher
".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto run_venv
:try_launcher
where py >nul 2>&1
if %errorlevel% neq 0 goto try_path
py -3.12 -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto run_312
py -3.13 -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto run_313
py -3.11 -c "import sys" >nul 2>&1
if %errorlevel% equ 0 goto run_311
:try_path
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if %errorlevel% neq 0 goto missing
python "context_menu.py" --remove
goto check
:run_venv
".venv\Scripts\python.exe" "context_menu.py" --remove
goto check
:run_312
py -3.12 "context_menu.py" --remove
goto check
:run_313
py -3.13 "context_menu.py" --remove
goto check
:run_311
py -3.11 "context_menu.py" --remove
:check
if %errorlevel% neq 0 goto failed
echo Context menu entries removed.
pause
exit /b 0
:missing
echo ERROR: Python missing. Install Python or use regedit to remove entries.
:failed
echo ERROR: Could not remove context menu entries.
pause
exit /b 1
