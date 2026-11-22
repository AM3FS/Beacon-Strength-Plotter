@echo off
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
    set "PYTHON=python"
) else (
    set "PYTHON=py -3"
)
%PYTHON% -c "import tkinter, serial, inspect, playwright.sync_api; from PIL import ImageGrab; assert 'window' in inspect.signature(ImageGrab.grab).parameters" >nul 2>&1
if errorlevel 1 %PYTHON% -m pip install -r requirements.txt
%PYTHON% plotter.py
if errorlevel 1 pause
