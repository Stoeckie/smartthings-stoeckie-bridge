@echo off
rem Update the bridge on the other pc with the files in this folder, using the saved login (see install-remote.bat).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-remote.ps1" -Unattended
