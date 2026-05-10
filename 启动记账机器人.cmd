@echo off
setlocal
cd /d "%~dp0"

%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_bookkeeping_robot.ps1"

if errorlevel 1 (
  echo.
  echo Startup failed. Please check Python and dependencies.
  echo First run command:
  echo powershell -ExecutionPolicy Bypass -File .\start_bookkeeping_robot.ps1 -InstallDeps
  pause
)
