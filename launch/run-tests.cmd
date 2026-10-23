@echo off
rem Runs the automated tests. Add -m integration to run only the real Ollama tests.
cd /d "%~dp0.."
".venv\Scripts\python.exe" -m pytest -q %*
pause
