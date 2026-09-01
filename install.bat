@echo off
setlocal enabledelayedexpansion
title anydl - setup
cd /d "%~dp0"

set "PY_VER=3.13.7"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "FF_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

echo ==========================================================
echo    anydl - setup
echo    Installs Python, yt-dlp and ffmpeg if they are missing.
echo ==========================================================
echo.

rem ---------------------------------------------------------------- Python
echo [1/3] Looking for Python...
call :find_python

if defined PY (
    echo       Found: !PY!
    goto have_python
)

echo       Not found. Installing Python %PY_VER%...
echo.

where winget >nul 2>nul
if not errorlevel 1 (
    echo       Trying winget...
    winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements --disable-interactivity
)

call :find_python
if defined PY goto have_python

echo       Downloading the official installer from python.org (~29 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri '%PY_URL%' -OutFile \"$env:TEMP\python-setup.exe\" -UseBasicParsing"
if errorlevel 1 (
    echo.
    echo   [ERROR] Could not download Python. Check your connection, or install it
    echo           manually from https://www.python.org/downloads/
    goto failed
)

echo       Installing (this can take a minute, no window will appear)...
rem Include_tcltk=1 is mandatory: the graphical interface needs tkinter.
start /wait "" "%TEMP%\python-setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_tcltk=1
del /q "%TEMP%\python-setup.exe" 2>nul

call :find_python
if not defined PY (
    echo.
    echo   [ERROR] Python was installed but could not be located.
    echo           Close this window, open it again and re-run install.bat
    goto failed
)
echo       Python installed: !PY!

:have_python
echo.

rem ---------------------------------------------------------------- yt-dlp
echo [2/3] Installing/updating yt-dlp...
"!PY!" -m pip install --upgrade --quiet pip
"!PY!" -m pip install --upgrade --quiet yt-dlp
if errorlevel 1 (
    echo   [ERROR] Could not install yt-dlp.
    goto failed
)

rem No `for /f` here: when the command starts with a quote cmd strips it, which
rem breaks any Python path containing spaces. Write to a file and read it back.
set "YTV="
"!PY!" -c "import yt_dlp;print(yt_dlp.version.__version__)" > "%TEMP%\ytv.txt" 2>nul
if exist "%TEMP%\ytv.txt" set /p YTV=<"%TEMP%\ytv.txt"
del /q "%TEMP%\ytv.txt" 2>nul
if not defined YTV (
    echo   [ERROR] yt-dlp does not import after installing.
    goto failed
)
echo       yt-dlp !YTV! ready.
echo.

rem ---------------------------------------------------------------- ffmpeg
echo [3/3] Looking for ffmpeg...
set "FF="
where ffmpeg >nul 2>nul
if not errorlevel 1 set "FF=PATH"
if exist "%~dp0bin\ffmpeg.exe" set "FF=bin"

if defined FF (
    echo       Found (!FF!^).
    goto have_ffmpeg
)

echo       Not found. Without it the app is capped at ~720p and cannot make MP3s.
where winget >nul 2>nul
if not errorlevel 1 (
    echo       Installing via winget...
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements --disable-interactivity
    where ffmpeg >nul 2>nul
    if not errorlevel 1 (
        echo       ffmpeg installed.
        goto have_ffmpeg
    )
)

echo       Downloading ffmpeg straight into bin\ (~80 MB)...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
  "$zip=\"$env:TEMP\ffmpeg.zip\"; $tmp=\"$env:TEMP\ffmpeg_x\";" ^
  "Invoke-WebRequest -Uri '%FF_URL%' -OutFile $zip -UseBasicParsing;" ^
  "if (Test-Path $tmp) { Remove-Item -Recurse -Force $tmp };" ^
  "Expand-Archive -Path $zip -DestinationPath $tmp -Force;" ^
  "$src = Get-ChildItem -Path $tmp -Recurse -Filter ffmpeg.exe ^| Select-Object -First 1;" ^
  "New-Item -ItemType Directory -Force -Path '%~dp0bin' ^| Out-Null;" ^
  "Copy-Item (Join-Path $src.Directory '*.exe') -Destination '%~dp0bin' -Force;" ^
  "Remove-Item -Recurse -Force $tmp, $zip"

if exist "%~dp0bin\ffmpeg.exe" (
    echo       ffmpeg installed into bin\.
) else (
    echo       [WARNING] Could not install ffmpeg. The app still works, but without
    echo                 MP3 output and without resolutions above 720p.
)

:have_ffmpeg
rem Remember where Python lives so run.bat works even before PATH refreshes.
> "%~dp0.python_path" echo !PY!

echo.
echo ==========================================================
echo    All set. Run run.bat to start the app.
echo ==========================================================
echo.
rem /t 15 /d N so an unattended run never hangs waiting for input.
choice /c YN /n /t 15 /d N /m "Start the app now? [Y/N] "
if errorlevel 2 goto done
start "" "%~dp0run.bat"

:done
endlocal
exit /b 0

:failed
echo.
pause
endlocal
exit /b 1

rem =============================================================== functions
:find_python
set "PY="
for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do call :accept "%%i"
if defined PY exit /b 0
for /f "delims=" %%i in ('python -c "import sys;print(sys.executable)" 2^>nul') do call :accept "%%i"
if defined PY exit /b 0
for %%p in (
    "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    "%ProgramFiles%\Python313\python.exe"
    "%ProgramFiles%\Python312\python.exe"
) do call :accept "%%~p"
exit /b 0

:accept
rem Reject the WindowsApps stub, which only opens the Microsoft Store.
if defined PY exit /b 0
if not exist "%~1" exit /b 0
echo %~1 | find /i "WindowsApps" >nul
if not errorlevel 1 exit /b 0
set "PY=%~1"
exit /b 0
