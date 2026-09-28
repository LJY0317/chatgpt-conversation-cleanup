@echo off
setlocal
cd /d "%~dp0"
set PYTHONDONTWRITEBYTECODE=1
where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
  py -3 run.py %*
) else (
  python run.py %*
)
