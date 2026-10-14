@echo off
cd /d "%~dp0"
where uv >nul 2>nul
if not errorlevel 1 (
    uv run app.py
    if errorlevel 1 pause
    exit /b
)
if not exist ".venv\Scripts\python.exe" (
    echo Run install.cmd first. See README.md for instructions.
    pause
    exit /b 1
)
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
