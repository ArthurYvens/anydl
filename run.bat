@echo off
setlocal enabledelayedexpansion
title YTConverter
cd /d "%~dp0"

rem 1) Path recorded by install.bat, so a stale PATH cannot break the launch.
set "PY="
if exist "%~dp0.python_path" (
    for /f "usebackq delims=" %%i in ("%~dp0.python_path") do (
        if exist "%%i" set "PY=%%i"
    )
)

rem 2) Otherwise look it up on the system.
if not defined PY (
    for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
        if exist "%%i" set "PY=%%i"
    )
)
if not defined PY (
    for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do (
        echo %%i | find /i "WindowsApps" >nul || if exist "%%i" set "PY=%%i"
    )
)

rem 3) Still nothing: hand over to the installer.
if not defined PY (
    echo Python not found. Starting the automatic setup...
    echo.
    call "%~dp0install.bat"
    exit /b
)

rem A missing yt-dlp also falls back to the installer.
"!PY!" -c "import yt_dlp" 2>nul
if errorlevel 1 (
    echo Installing yt-dlp...
    "!PY!" -m pip install --upgrade yt-dlp
    "!PY!" -c "import yt_dlp" 2>nul
    if errorlevel 1 (
        call "%~dp0install.bat"
        exit /b
    )
)

rem pythonw keeps the console window from sitting behind the app.
set "PYW=!PY:python.exe=pythonw.exe!"
if not exist "!PYW!" set "PYW=!PY!"

start "" "!PYW!" "%~dp0ytconverter.py" %*
endlocal
