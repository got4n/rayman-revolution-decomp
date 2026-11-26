@echo off
cd /d "%~dp0.."
if exist .venv\Scripts\python.exe (
    .venv\Scripts\python.exe tools\objdiff_build.py %*
) else (
    python tools\objdiff_build.py %*
)
exit /b %errorlevel%
