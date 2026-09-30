@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Install Python 3.12+ and run:
  echo python -m venv .venv
  echo .venv\Scripts\python.exe -m pip install -e .
  pause
  exit /b 1
)
".venv\Scripts\python.exe" scripts\desktop.py
if errorlevel 1 pause
