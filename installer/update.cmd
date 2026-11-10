@echo off
setlocal EnableExtensions
title Update Local AI Switchboard
cd /d "%~dp0"

set "SCRIPT=%~dp0update.ps1"
set "INSTALL="
set "SOURCE="

if not "%~1"=="" set "INSTALL=%~1"

if not defined INSTALL if exist "%~dp0..\venv\Scripts\python.exe" if exist "%~dp0..\.env" set "INSTALL=%~dp0.."
if not defined INSTALL if exist "%USERPROFILE%\AI\local-ai-switchboard\venv\Scripts\python.exe" set "INSTALL=%USERPROFILE%\AI\local-ai-switchboard"
if not defined INSTALL if exist "D:\AI\LocalAISwitchboard\venv\Scripts\python.exe" set "INSTALL=D:\AI\LocalAISwitchboard"

if exist "%~dp0..\pyproject.toml" (
  for %%I in ("%~dp0..") do set "SOURCE=%%~fI"
)
if not defined SOURCE if defined INSTALL if exist "%INSTALL%\config\source-root.txt" set /p SOURCE=<"%INSTALL%\config\source-root.txt"

if not exist "%SCRIPT%" (
  echo update.ps1 was not found next to this file.
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
if not defined SOURCE (
  echo The source tree was not found.
  echo Put this file in the installer folder of the project that contains pyproject.toml.
  pause
  exit /b 1
)
if not exist "%SOURCE%\pyproject.toml" (
  echo pyproject.toml was not found in:
  echo   %SOURCE%
  pause
  exit /b 1
)

echo Install: %INSTALL%
echo Source:  %SOURCE%
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT%" -InstallRoot "%INSTALL%" -SourceRoot "%SOURCE%"
set "CODE=%ERRORLEVEL%"
echo.
echo Exit code: %CODE%
pause
exit /b %CODE%
