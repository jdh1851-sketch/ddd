@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt pyinstaller
if errorlevel 1 exit /b 1
py -m PyInstaller --noconfirm --clean --onefile --windowed --name Kkullim --icon app_icon.ico --add-data "app_icon.ico;." --add-data "tessdata;tessdata" --add-data "..\assets\templates;templates" kkullim_pc.py
if errorlevel 1 exit /b 1
echo Build complete: dist\Kkullim.exe
pause
