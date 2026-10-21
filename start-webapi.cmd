@echo off
setlocal
cd /d "%~dp0" || exit /b 1

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Install Python 3.12+ and run:
  echo uv sync --extra server --extra dev
  pause
  exit /b 1
)

if not exist "server.config.json" (
  echo Missing server.config.json. Copy server.config.example.json to server.config.json
  echo and configure the database connection, token and encryption key.
  pause
  exit /b 1
)

echo Checking for an existing image-agent Web API...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-webapi.ps1" -PythonExecutable "%~dp0.venv\Scripts\python.exe"
if errorlevel 1 (
  echo Failed to stop the existing Web API. Startup cancelled.
  pause
  exit /b 1
)

echo Starting image-agent Web API. Press Ctrl+C to stop.
"%~dp0.venv\Scripts\python.exe" -m image_agent.server
set "webapi_exit_code=%errorlevel%"
if not "%webapi_exit_code%"=="0" (
  echo Web API exited with code %webapi_exit_code%.
  pause
)
exit /b %webapi_exit_code%
