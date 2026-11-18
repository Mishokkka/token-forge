@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto bad_directory
if not errorlevel 0 goto bad_directory
title TokenForge - Remove context menu
if not exist ".venv\Scripts\python.exe" goto try_launcher
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto run_venv
:try_launcher
where py >nul 2>&1
if errorlevel 1 goto try_path
if not errorlevel 0 goto try_path
py -3.12 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto run_312
py -3.13 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto run_313
py -3.11 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto run_311
py -3.10 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto run_310
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto run_generic
:try_path
python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
if errorlevel 1 goto missing
if not errorlevel 0 goto missing
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
goto check
:run_310
py -3.10 "context_menu.py" --remove
goto check
:run_generic
py -3 "context_menu.py" --remove
:check
if errorlevel 1 goto failed
if not errorlevel 0 goto failed
echo Context menu entries removed.
pause
exit /b 0
:missing
echo ERROR: Python missing. Press Win+R, enter shell:sendto and press Enter.
echo Delete only the four shortcuts whose names begin with TokenForge -.
echo Older registry entries need separate cleanup; reinstall Python to run this script.
:failed
echo ERROR: Could not remove context menu entries.
pause
exit /b 1

:bad_directory
echo ERROR: Could not enter the program directory.
pause
exit /b 1
