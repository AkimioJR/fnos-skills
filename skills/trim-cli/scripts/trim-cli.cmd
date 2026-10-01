@echo off
setlocal

call :reject_media %*
if errorlevel 1 exit /b %errorlevel%

set "SCRIPT_DIR=%~dp0"
set "ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "ARCH=%PROCESSOR_ARCHITEW6432%"
if /I "%ARCH%"=="ARM64" (
  set "TARGET=%SCRIPT_DIR%..\bin\trim-cli-windows-arm64.exe"
) else (
  set "TARGET=%SCRIPT_DIR%..\bin\trim-cli-windows-x64.exe"
)

if not exist "%TARGET%" (
  echo Missing packaged binary: %TARGET% 1>&2
  exit /b 1
)

"%TARGET%" %*
exit /b %errorlevel%

:reject_media
if "%~1"=="" exit /b 0
set "ARG=%~1"
if /I "%ARG%"=="--host" shift&shift&goto reject_media
if /I "%ARG%"=="--port" shift&shift&goto reject_media
if /I "%ARG%"=="--profile" shift&shift&goto reject_media
if /I "%ARG%"=="--scheme" shift&shift&goto reject_media
if "%ARG:~0,1%"=="-" shift&goto reject_media
if /I "%ARG%"=="media" (
  echo Media commands are not available in this skill package. 1>&2
  exit /b 2
)
exit /b 0
