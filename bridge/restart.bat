@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart.ps1"
timeout /t 8 >nul
