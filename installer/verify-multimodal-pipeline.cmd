@echo off
setlocal EnableExtensions
title Multimodal Qwen and FLUX check
cd /d "%~dp0"

set "SCRIPT=%~dp0verify-multimodal-pipeline.ps1"
set "INSTALL="

if not "%~1"=="" set "INSTALL=%~1"

if not defined INSTALL if exist "%~dp0..\venv\Scripts\python.exe" if exist "%~dp0..\.env" set "INSTALL=%~dp0.."
if not defined INSTALL if exist "%USERPROFILE%\AI\local-ai-switchboard\venv\Scripts\python.exe" set "INSTALL=%USERPROFILE%\AI\local-ai-switchboard"
if not defined INSTALL if exist "D:\AI\LocalAISwitchboard\venv\Scripts\python.exe" set "INSTALL=D:\AI\LocalAISwitchboard"

if not exist "%SCRIPT%" (
  echo verify-multimodal-pipeline.ps1 was not found next to this file.
  pause
  exit /b 1
)
if not defined INSTALL (
  echo No Local AI Switchboard install was found.
  pause
  exit /b 1
)

echo Install: %INSTALL%
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -InstallRoot "%INSTALL%"
set "CODE=%ERRORLEVEL%"
echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
