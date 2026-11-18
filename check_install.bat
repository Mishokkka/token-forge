@echo off
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
if errorlevel 1 goto bad_directory
if not errorlevel 0 goto bad_directory
title TokenForge - Check installation
echo =========================================================
echo  TOKENFORGE - DIAGNOSTICS
 echo =========================================================
echo.
where py >nul 2>&1
if errorlevel 1 goto no_launcher
if not errorlevel 0 goto no_launcher
py -0p

goto env_check

:no_launcher
echo Python launcher is not available, checking virtual environment.

:env_check
if not exist ".venv\Scripts\python.exe" goto no_env
".venv\Scripts\python.exe" --version
".venv\Scripts\python.exe" -c "import PIL, rembg, onnxruntime, tkinterdnd2, tkinter; print('Python packages: OK')"
if errorlevel 1 goto check_failed
if not errorlevel 0 goto check_failed
if not exist "diagnostics_tests.py" goto tests_missing
".venv\Scripts\python.exe" "diagnostics_tests.py"
if errorlevel 2 goto tests_incomplete
if errorlevel 1 goto check_failed
if not errorlevel 0 goto check_failed
echo.
echo SUCCESS: Python packages, image engine and CLI tests passed.
echo.
pause
exit /b 0

:tests_missing
echo Python imports passed, but the diagnostics test runner is missing.
:tests_incomplete
echo Regression tests did not complete. Download the full source ZIP.
pause
exit /b 2

:no_env
echo ERROR: .venv was not created. Run install.bat.

:check_failed
echo ERROR: Installation check failed. See details above.
pause
exit /b 1

:bad_directory
echo ERROR: Could not enter the program directory.
pause
exit /b 1
