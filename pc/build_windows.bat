@echo off
cd /d "%~dp0"
py -m pip install -r requirements.txt pyinstaller==6.16.0 pytest
if errorlevel 1 exit /b 1
py -m pytest tests -q
if errorlevel 1 exit /b 1
powershell -NoProfile -ExecutionPolicy Bypass -File packaging\build_release.ps1
if errorlevel 1 exit /b 1
echo Single portable EXE is in the release folder.
pause
