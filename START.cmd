@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "launcher.py"
) else (
    python "launcher.py"
    if errorlevel 1 pause
)
