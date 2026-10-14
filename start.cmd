@echo off
cd /d "%~dp0"
set UV_CACHE_DIR=%~dp0.work\uv-cache
set UV_PROJECT_ENVIRONMENT=%~dp0.venv
if exist ".venv\Scripts\python.exe" goto run_python
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
:run_python
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
".venv\Scripts\python.exe" app.py
if errorlevel 1 pause
