@echo off
rem Double-click to set up Enoy (creates .venv and shortcuts). Safe to run again.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0launch\setup.ps1"
pause
