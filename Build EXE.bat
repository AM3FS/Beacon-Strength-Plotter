@echo off
setlocal
cd /d "%~dp0"
set "PLOTTER_PYTHON=py -3"
where py >nul 2>&1
if errorlevel 1 set "PLOTTER_PYTHON=python"

%PLOTTER_PYTHON% -c "import tkinter; import struct; assert struct.calcsize('P') == 8" >nul 2>&1
if errorlevel 1 (
    echo Install 64-bit Python 3 for Windows with Tcl/Tk, then run this batch file again.
    pause
    exit /b 1
)

if not exist ".build-venv\Scripts\python.exe" (
    %PLOTTER_PYTHON% -m venv ".build-venv"
    if errorlevel 1 goto failed
)
set "BUILD_PYTHON=.build-venv\Scripts\python.exe"
"%BUILD_PYTHON%" -m pip install -r requirements.txt pyinstaller
if errorlevel 1 goto failed

"%BUILD_PYTHON%" -m PyInstaller --noconfirm --onefile --windowed --icon "radio_tower.ico" --add-data "radio_tower.ico;." --add-data "beacon_background_day.png;." --add-data "beacon_background_night.png;." --add-data "OpenSans-Bold.ttf;." --add-data "OpenSans-LICENSE.txt;." --add-data "station_countries.json;." --name "Icom S-meter Plotter" plotter.py
if errorlevel 1 goto failed
if not exist "dist\Icom S-meter Plotter.exe" goto failed

echo.
echo Built "%CD%\dist\Icom S-meter Plotter.exe"
echo The EXE can run on Windows without a separate Python installation.
pause
exit /b 0

:failed
echo.
echo The build failed. Check the messages above.
pause
exit /b 1
