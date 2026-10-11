@echo off
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel% equ 0 (
    py -3 desktop.py
) else (
    python desktop.py
)
if %errorlevel% neq 0 (
    echo Check the error above. Python 3.10 or later with Tcl/Tk support is required.
    echo For Windows timezones run: py -3 -m pip install -r requirements.txt
    pause
)
