@echo off
rem Development launcher: runs Enoy with a console window and verbose logging.
rem   dev.cmd            native window, logs in this console
rem   dev.cmd --browser  open in the default browser instead
rem   dev.cmd --no-window --port 8765   server only, for UI work in any browser
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m fusion_ai --verbose %*
