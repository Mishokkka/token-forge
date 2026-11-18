@echo off
setlocal EnableExtensions
cd /d "%~dp0"
if errorlevel 1 goto bad_directory
if not errorlevel 0 goto bad_directory
title TokenForge - Setup

echo =========================================================
echo  TOKENFORGE - INSTALL (Windows, Python 3.11 - 3.13)
echo =========================================================
echo.

if not exist ".venv" goto find_python
if not exist ".venv\Scripts\python.exe" goto stale_env
".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 goto stale_env
if not errorlevel 0 goto stale_env
goto install_packages

:stale_env
echo ERROR: Existing .venv cannot run or is incompatible.
echo Rename or delete only the .venv folder, then run install.bat again.
echo Your settings and cutouts are stored separately in LocalAppData.
goto failed

:find_python

where py >nul 2>&1
if errorlevel 1 goto try_python_cmd
if not errorlevel 0 goto try_python_cmd

py -3.12 -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto make_312
py -3.13 -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto make_313
py -3.11 -c "import sys; sys.exit(0 if (3,11) <= sys.version_info[:2] <= (3,13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if not errorlevel 1 if errorlevel 0 goto make_311

goto try_python_cmd

:make_312
echo Found Python 3.12.
py -3.12 -m venv ".venv"
if errorlevel 1 goto venv_failed
if not errorlevel 0 goto venv_failed
goto install_packages

:make_313
echo Found Python 3.13.
py -3.13 -m venv ".venv"
if errorlevel 1 goto venv_failed
if not errorlevel 0 goto venv_failed
goto install_packages

:make_311
echo Found Python 3.11.
py -3.11 -m venv ".venv"
if errorlevel 1 goto venv_failed
if not errorlevel 0 goto venv_failed
goto install_packages

:try_python_cmd
python -c "import sys; exit(0 if (3, 11) <= sys.version_info[:2] <= (3, 13) and sys.maxsize > 2**32 else 1)" >nul 2>&1
if errorlevel 1 goto python_missing
if not errorlevel 0 goto python_missing
echo Found compatible 64-bit Python.
python -m venv ".venv"
if errorlevel 1 goto venv_failed
if not errorlevel 0 goto venv_failed
goto install_packages

:python_missing
echo ERROR: Python 3.11, 3.12 or 3.13 (64-bit) not found.
echo Install Python 3.12 64-bit from https://www.python.org/downloads/release/python-31210/
echo During installation check Add python.exe to PATH, then reopen this file.
goto failed

:venv_failed
echo ERROR: Could not create the virtual environment.
echo Check that Python is installed with pip and venv support.
echo If a partial .venv directory was created, rename it before retrying install.bat.
goto failed

:install_packages
if not exist ".venv\Scripts\python.exe" goto venv_failed
echo.
echo Installing required Python packages (internet required)...
echo.
".venv\Scripts\python.exe" -m pip --version
if not errorlevel 1 if errorlevel 0 goto pip_ready
echo Bootstrapping pip in the existing virtual environment...
".venv\Scripts\python.exe" -m ensurepip --upgrade
if errorlevel 1 goto pip_missing
if not errorlevel 0 goto pip_missing
".venv\Scripts\python.exe" -m pip --version
if errorlevel 1 goto pip_missing
if not errorlevel 0 goto pip_missing

:pip_ready
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto failed
if not errorlevel 0 goto failed
".venv\Scripts\python.exe" -m pip install -r "requirements.txt"
if errorlevel 1 goto failed
if not errorlevel 0 goto failed

echo.
echo Verifying installed packages...
".venv\Scripts\python.exe" -c "import PIL, rembg, onnxruntime, tkinterdnd2, tkinter; print('Imports: OK')"
if errorlevel 1 goto import_failed
if not errorlevel 0 goto import_failed

echo.
echo SUCCESS! Installation completed.
echo Next: double-click run.bat to launch TokenForge.
echo Models download on first use; you need internet for that once.
pause
exit /b 0

:import_failed
echo ERROR: A required package could not be imported.
echo If ONNX Runtime cannot load, install the Microsoft Visual C++ 2015-2022 x64 Redistributable.
echo https://aka.ms/vs/17/release/vc_redist.x64.exe
echo Then run install.bat again.
goto failed

:pip_missing
echo ERROR: pip was not found in the virtual environment.
echo Rename only the .venv folder and run install.bat again.
echo Your settings and cutouts are stored separately in LocalAppData.
goto failed

:failed
echo.
echo ERROR: Installation failed. See error details above.
echo Send a screenshot of the FIRST error message, not just the last line.
pause
exit /b 1

:bad_directory
echo ERROR: Could not enter the program directory.
pause
exit /b 1
