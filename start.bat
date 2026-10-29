@echo off
cd /d "%~dp0"
if not exist .venv (py -3 -m venv .venv 2>nul || python -m venv .venv)
call .venv\Scripts\activate.bat
python -m pip install -q -e ".[dev]"
python -m playwright install chromium
python main.py ui
pause
