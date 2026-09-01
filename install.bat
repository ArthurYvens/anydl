@echo off
setlocal enabledelayedexpansion
title anydl - setup
cd /d "%~dp0"

set "PY_VER=3.13.7"
set "PY_URL=https://www.python.org/ftp/python/%PY_VER%/python-%PY_VER%-amd64.exe"
set "FF_URL=https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

call :setup_colors

echo(
echo(  %C_LINE%============================================%C_OFF%
echo(  %C_LINE%^|%C_OFF%  %C_TITLE%anydl%C_OFF%                                   %C_LINE%^|%C_OFF%
echo(  %C_LINE%^|%C_OFF%  %C_DIM%video and audio downloader%C_OFF%              %C_LINE%^|%C_OFF%
echo(  %C_LINE%============================================%C_OFF%
echo(

rem ---------------------------------------------------------------- Python
call :step 1 3 "Python"
call :find_python

if defined PY (
    call :ok "found  !PY!"
    goto have_python
)

call :warn "not installed"

where winget >nul 2>nul
if not errorlevel 1 (
    call :info "installing Python %PY_VER% with winget"
    winget install --id Python.Python.3.13 -e --accept-package-agreements --accept-source-agreements --disable-interactivity >nul 2>nul
    call :find_python
)
if defined PY (
    call :ok "installed  !PY!"
    goto have_python
)

call :info "downloading the installer from python.org (~29 MB)"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop'; $ProgressPreference='SilentlyContinue';" ^
  "Invoke-WebRequest -Uri '%PY_URL%' -OutFile \"$env:TEMP\python-setup.exe\" -UseBasicParsing"
if errorlevel 1 (
    call :fail "could not download Python"
    call :hint "install it by hand from https://www.python.org/downloads/"
    goto failed
)

rem Include_tcltk=1 is mandatory: the graphical interface needs tkinter.
call :info "installing quietly, this takes about a minute"
start /wait "" "%TEMP%\python-setup.exe" /quiet InstallAllUsers=0 PrependPath=1 Include_pip=1 Include_tcltk=1
del /q "%TEMP%\python-setup.exe" 2>nul

call :find_python
if not defined PY (
    call :fail "Python installed but could not be located"
    call :hint "close this window, open it again and re-run install.bat"
    goto failed
)
call :ok "installed  !PY!"

:have_python
echo(

rem ---------------------------------------------------------------- yt-dlp
call :step 2 3 "yt-dlp"
call :info "installing with pip"
"!PY!" -m pip install --upgrade --quiet pip
"!PY!" -m pip install --upgrade --quiet yt-dlp
if errorlevel 1 (
    call :fail "pip could not install yt-dlp"
    goto failed
)

rem No `for /f` here: when the command starts with a quote cmd strips it, which
rem breaks any Python path containing spaces. Write to a file and read it back.
set "YTV="
"!PY!" -c "import yt_dlp;print(yt_dlp.version.__version__)" > "%TEMP%\ytv.txt" 2>nul
if exist "%TEMP%\ytv.txt" set /p YTV=<"%TEMP%\ytv.txt"
del /q "%TEMP%\ytv.txt" 2>nul
if not defined YTV (
    call :fail "yt-dlp does not import after installing"
    goto failed
)
call :ok "yt-dlp !YTV!"
echo(

rem ---------------------------------------------------------------- ffmpeg
call :step 3 3 "ffmpeg"
set "FF="
where ffmpeg >nul 2>nul
if not errorlevel 1 set "FF=on PATH"
if exist "%~dp0bin\ffmpeg.exe" set "FF=in bin\"

if defined FF (
    call :ok "found  !FF!"
    goto have_ffmpeg
)

call :warn "not installed"
call :hint "without it: capped at ~720p, and no MP3 output"

where winget >nul 2>nul
if not errorlevel 1 (
    call :info "installing with winget"
    winget install --id Gyan.FFmpeg -e --accept-package-agreements --accept-source-agreements --disable-interactivity >nul 2>nul
    where ffmpeg >nul 2>nul
    if not errorlevel 1 (
        call :ok "installed"
        goto have_ffmpeg
    )
)

call :info "downloading a build into bin\ (~80 MB)"
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
    call :ok "installed into bin\"
) else (
    call :warn "could not install ffmpeg"
    call :hint "anydl still works, just without MP3 and above 720p"
)

:have_ffmpeg
rem Remember where Python lives so run.bat works even before PATH refreshes.
> "%~dp0.python_path" echo !PY!

echo(
echo(  %C_OK%============================================%C_OFF%
echo(   %C_OK%Ready.%C_OFF%  Run %C_TITLE%run.bat%C_OFF% to start anydl.
echo(  %C_OK%============================================%C_OFF%
echo(

rem /t 15 /d N so an unattended run never hangs waiting for input.
choice /c YN /n /t 15 /d N /m "  Start anydl now? [Y/N] "
if errorlevel 2 goto done
start "" "%~dp0run.bat"

:done
endlocal
exit /b 0

:failed
echo(
echo(  %C_FAIL%Setup stopped.%C_OFF%  Nothing was left half-installed.
echo(
pause
endlocal
exit /b 1

rem =============================================================== output
:setup_colors
rem Pull a real ESC character out of cmd; without it, print plain text.
set "ESC="
for /f %%a in ('echo prompt $E ^| cmd') do set "ESC=%%a"
if defined NO_COLOR set "ESC="
if defined ESC (
    set "C_TITLE=%ESC%[1;96m"
    set "C_LINE=%ESC%[38;5;99m"
    set "C_STEP=%ESC%[96m"
    set "C_OK=%ESC%[92m"
    set "C_WARN=%ESC%[93m"
    set "C_FAIL=%ESC%[91m"
    set "C_DIM=%ESC%[90m"
    set "C_OFF=%ESC%[0m"
) else (
    set "C_TITLE=" & set "C_LINE=" & set "C_STEP=" & set "C_OK="
    set "C_WARN=" & set "C_FAIL=" & set "C_DIM=" & set "C_OFF="
)
exit /b 0

:step
echo(  %C_STEP%[%~1/%~2]%C_OFF% %C_TITLE%%~3%C_OFF%
exit /b 0

:ok
echo(        %C_OK%ok%C_OFF%   %~1
exit /b 0

:info
echo(        %C_DIM%..%C_OFF%   %C_DIM%%~1%C_OFF%
exit /b 0

:warn
echo(        %C_WARN%!!%C_OFF%   %~1
exit /b 0

:fail
echo(        %C_FAIL%xx%C_OFF%   %~1
exit /b 0

:hint
echo(             %C_DIM%%~1%C_OFF%
exit /b 0

rem =============================================================== helpers
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
