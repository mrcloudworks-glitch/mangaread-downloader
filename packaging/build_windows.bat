@echo off
REM Build a single-file Windows executable with PyInstaller.
REM Run from the repository root:  packaging\build_windows.bat
setlocal
cd /d "%~dp0.."

python -m pip install --upgrade pyinstaller requests beautifulsoup4 Pillow || goto :fail

pyinstaller --noconfirm --clean ^
  --onefile ^
  --windowed ^
  --name "MangaReadDownloader" ^
  --icon "assets\icon.ico" ^
  --add-data "mangaread_downloader\assets;assets" ^
  run.py || goto :fail

echo.
echo Done! Find it at  dist\MangaReadDownloader.exe
echo Tip: copy the exe anywhere; no Python installation is required on the target machine.
goto :eof

:fail
echo.
echo Build failed. See the messages above.
exit /b 1
