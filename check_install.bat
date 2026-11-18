@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if %errorlevel% neq 0 (
    echo ERROR: Could not enter the program directory.
    pause
    exit /b 1
)
title TokenForge - Check installation
echo =========================================================
echo  TOKENFORGE - DIAGNOSTICS
 echo =========================================================
echo.
where py >nul 2>&1
if %errorlevel% neq 0 goto no_launcher
py -0p

goto env_check

:no_launcher
echo Python launcher is not available, checking virtual environment.

:env_check
if not exist ".venv\Scripts\python.exe" goto no_env
".venv\Scripts\python.exe" --version
".venv\Scripts\python.exe" -c "import PIL, rembg, onnxruntime, tkinterdnd2, tkinter; print('Python packages: OK')"
if %errorlevel% neq 0 goto check_failed
".venv\Scripts\python.exe" -m unittest "test_engine.py" "test_cache.py" "test_cli.py" "test_items.py"
if %errorlevel% neq 0 goto check_failed
echo.
echo SUCCESS: Python packages, image engine and CLI tests passed.
echo.
pause
exit /b 0

:no_env
echo ERROR: .venv was not created. Run install.bat.

:check_failed
echo ERROR: Installation check failed. See details above.
pause
exit /b 1
