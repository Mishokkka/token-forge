@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title TokenForge - Setup

echo =========================================================
echo  TOKENFORGE - INSTALL (Windows, Python 3.11 - 3.13)
echo =========================================================
echo.

if exist ".venv\Scripts\python.exe" goto install_packages

where py >nul 2>&1
if errorlevel 1 goto try_python_cmd

py -3.12 -V >nul 2>&1
if not errorlevel 1 goto make_312
py -3.13 -V >nul 2>&1
if not errorlevel 1 goto make_313
py -3.11 -V >nul 2>&1
if not errorlevel 1 goto make_311

goto try_python_cmd

:make_312
echo Found Python 3.12.
py -3.12 -m venv ".venv"
if errorlevel 1 goto venv_failed
goto install_packages

:make_313
echo Found Python 3.13.
py -3.13 -m venv ".venv"
if errorlevel 1 goto venv_failed
goto install_packages

:make_311
echo Found Python 3.11.
py -3.11 -m venv ".venv"
if errorlevel 1 goto venv_failed
goto install_packages

:try_python_cmd
python -c "import sys; exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
echo Found compatible 64-bit Python.
python -m venv ".venv"
if errorlevel 1 goto venv_failed
goto install_packages

:python_missing
echo ERROR: Python 3.11, 3.12 or 3.13 (64-bit) not found.
echo Install Python 3.12 64-bit from https://www.python.org/downloads/release/python-31210/
echo During installation check Add python.exe to PATH, then reopen this file.
goto failed

:venv_failed
echo ERROR: Could not create the virtual environment.
echo Check that Python is installed with pip and venv support.
goto failed

:install_packages
if not exist ".venv\Scripts\python.exe" goto venv_failed
echo.
echo Installing required Python packages (internet required)...
echo.
".venv\Scripts\python.exe" -m pip --version
if errorlevel 1 goto pip_missing
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto failed

echo.
echo Verifying installed packages...
".venv\Scripts\python.exe" -c "import PIL, rembg, onnxruntime, tkinterdnd2, tkinter; print('Imports: OK')"
if errorlevel 1 goto failed

echo.
echo SUCCESS! Installation completed.
echo Next: double-click run.bat to launch TokenForge.
echo Models download on first use; you need internet for that once.
pause
exit /b 0

:pip_missing
echo ERROR: pip was not found in the virtual environment.
echo Try installing Python 3.12 with pip enabled.
goto failed

:failed
echo.
echo ERROR: Installation failed. See error details above.
echo Send a screenshot of the FIRST error message, not just the last line.
pause
exit /b 1
