@echo off
setlocal EnableExtensions DisableDelayedExpansion
title TokenForge
if not exist "%~dp0.venv\Scripts\python.exe" goto need_install
"%~dp0.venv\Scripts\python.exe" "%~dp0tokenforge.py" %*
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

