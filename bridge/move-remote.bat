@echo off
rem Move the bridge from the target pc to Docker: stops it there and exports its data encrypted (see move-remote.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0move-remote.ps1" %*
type "%~dp0move-remote.txt"
