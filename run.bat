@echo off
REM MangaRead Downloader - double-click launcher for Windows.
REM First run: creates .venv\ and installs dependencies (needs internet once).
REM Every run: starts the GUI (with no console window).
setlocal EnableExtensions
cd /d "%~dp0"
title MangaRead Downloader

REM --- find Python 3.9+: py launcher first, then PATH -----------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  where python3 >nul 2>&1 && set "PY=python3"
)
if not defined PY goto :no_python

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" || goto :old_python
echo Using: %PY%

REM --- Tkinter check (ships with the python.org installer) -------------------
%PY% -c "import tkinter" >nul 2>&1 || goto :no_tkinter

REM --- create / reuse the virtual environment ---------------------------------
set "VENV=.venv"
set "VENV_PY=%VENV%\Scripts\python.exe"
set "VENV_PYW=%VENV%\Scripts\pythonw.exe"

if not exist "%VENV_PY%" (
  echo Creating virtual environment in %VENV% ...
  %PY% -m venv "%VENV%" || goto :venv_fail
)

REM --- install dependencies only when needed (marker vs requirements.txt) -----
set "MARKER=%VENV%\.deps-ok"
set "STALE="
if not exist "%MARKER%" set "STALE=1"
if not defined STALE (
  for %%I in ("requirements.txt") do set "REQ_TIME=%%~tI"
  for %%I in ("%MARKER%") do set "MARK_TIME=%%~tI"
  rem crude compare: reinstall if requirements.txt is newer
  powershell -NoProfile -Command "if ((Get-Item 'requirements.txt').LastWriteTime -gt (Get-Item '%MARKER%').LastWriteTime) { exit 1 } else { exit 0 }" || set "STALE=1"
)
if defined STALE (
  echo Installing dependencies ^(requests, beautifulsoup4, Pillow^) ...
  "%VENV_PY%" -m pip install --quiet --upgrade pip
  "%VENV_PY%" -m pip install --quiet -r requirements.txt || goto :deps_fail
  type nul > "%MARKER%"
)

REM --- launch (no console window if pythonw exists) ---------------------------
if exist "%VENV_PYW%" (
  start "" "%VENV_PYW%" "%~dp0run.py"
  exit /b 0
)
"%VENV_PY%" "%~dp0run.py"
if errorlevel 1 pause
exit /b 0

:no_python
echo.
echo Python 3.9+ was not found on this PC.
echo Install it from https://www.python.org/downloads/ ^(tick "tcl/tk and IDLE" and "Add python.exe to PATH"^),
echo then double-click run.bat again.
goto :end_pause

:old_python
echo.
echo Your Python is older than 3.9. Please install a newer Python from https://www.python.org/downloads/
goto :end_pause

:no_tkinter
echo.
echo Tkinter is missing. Reinstall Python from https://www.python.org/downloads/ and keep
echo the "tcl/tk and IDLE" feature ticked in the installer.
goto :end_pause

:venv_fail
echo.
echo Could not create the virtual environment. Make sure pip is included with your Python install.
goto :end_pause

:deps_fail
echo.
echo Dependency install failed. Are you connected to the internet?
goto :end_pause

:end_pause
pause
exit /b 1
