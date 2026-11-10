@echo off
setlocal
cd /d "%~dp0"

if not defined GROQ_API_KEY (
  echo GROQ_API_KEY is not set. Start Patchwork from PowerShell after setting it:
  echo   $env:GROQ_API_KEY = "your Groq API key"
  echo   .\start_patchwork.bat
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  python -m venv ".venv"
  if errorlevel 1 (
    echo Could not create the Python virtual environment. Install Python 3.10+ and retry.
    exit /b 1
  )
)

".venv\Scripts\python.exe" -m pip show groq >nul 2>nul
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo Could not install project requirements. Check your internet connection and retry.
    exit /b 1
  )
)

echo Starting Patchwork at http://127.0.0.1:5000
"%~dp0.venv\Scripts\python.exe" app.py
