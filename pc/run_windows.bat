@echo off
cd /d "%~dp0"
py kkullim_pc.py
if errorlevel 1 pause
