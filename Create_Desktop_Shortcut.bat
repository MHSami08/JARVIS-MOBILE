@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Create_Desktop_Shortcut.ps1"
echo.
pause
