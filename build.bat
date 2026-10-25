@echo off
setlocal
cd /d "%~dp0"
title Building SK Recorder
echo === SK Recorder - build ===

REM Pick a Python version that all dependencies support (3.11 - 3.13)
set PYCMD=
for %%V in (3.12 3.13 3.11) do (
  if not defined PYCMD (
    py -%%V --version >nul 2>nul && set PYCMD=py -%%V
  )
)
if not defined PYCMD (
  echo.
  echo No compatible Python found. Your Python is probably too new - the audio library needs 3.11, 3.12 or 3.13.
  echo.
  echo Install Python 3.12 once, then run this file again:
  echo    winget install Python.Python.3.12
  echo or download it from https://www.python.org/downloads/release/python-3128/
  echo.
  pause
  exit /b 1
)
echo Using: %PYCMD%

if exist .venv rmdir /s /q .venv
%PYCMD% -m venv .venv || (echo Could not create the environment. & pause & exit /b 1)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
pip install -r requirements.txt || (echo Dependency install failed. & pause & exit /b 1)

if not exist bin\ffmpeg.exe (
  echo Downloading FFmpeg so it can be bundled inside the exe...
  mkdir bin 2>nul
  powershell -NoProfile -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip' -OutFile ffmpeg.zip; Expand-Archive ffmpeg.zip -DestinationPath ffmpeg_tmp -Force; Copy-Item (Get-ChildItem ffmpeg_tmp -Recurse -Filter ffmpeg.exe | Select-Object -First 1).FullName bin\ffmpeg.exe; Remove-Item ffmpeg.zip,ffmpeg_tmp -Recurse -Force"
)

set ADDBIN=
if exist bin\ffmpeg.exe set ADDBIN=--add-binary "bin\ffmpeg.exe;bin"

pyinstaller --noconfirm --clean --onefile --windowed --name "SK Recorder" --icon assets\icon.ico --version-file version_info.txt ^
  --collect-submodules pynput --hidden-import pyaudiowpatch %ADDBIN% main.py
if errorlevel 1 (echo Build failed. & pause & exit /b 1)

echo.
echo Done:  dist\SK Recorder.exe
pause
