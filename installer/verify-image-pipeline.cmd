@echo off
setlocal EnableExtensions
title FLUX.2 Klein image pipeline check
cd /d "%~dp0"

set "SCRIPT=%~dp0verify-image-pipeline.ps1"
set "INSTALL="

if not "%~1"=="" set "INSTALL=%~1"

if not defined INSTALL if exist "%~dp0..\venv\Scripts\python.exe" if exist "%~dp0..\.env" set "INSTALL=%~dp0.."
if not defined INSTALL if exist "%USERPROFILE%\AI\local-ai-switchboard\venv\Scripts\python.exe" set "INSTALL=%USERPROFILE%\AI\local-ai-switchboard"
if not defined INSTALL if exist "D:\AI\LocalAISwitchboard\venv\Scripts\python.exe" set "INSTALL=D:\AI\LocalAISwitchboard"

if not exist "%SCRIPT%" (
  echo verify-image-pipeline.ps1 was not found next to this file.
  pause
  exit /b 1
)
if not defined INSTALL (
  echo No Local AI Switchboard install was found.
  echo Looked for venv\Scripts\python.exe in:
  echo   the parent of this folder
  echo   %USERPROFILE%\AI\local-ai-switchboard
  echo   D:\AI\LocalAISwitchboard
  echo.
  echo Drag the install folder onto this file, or run:
  echo   "%~f0" "D:\path\to\LocalAISwitchboard"
  pause
  exit /b 1
)

echo Install: %INSTALL%
echo Width 768, height 768, seed 42.
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -InstallRoot "%INSTALL%" -Width 768 -Height 768
set "CODE=%ERRORLEVEL%"
echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
