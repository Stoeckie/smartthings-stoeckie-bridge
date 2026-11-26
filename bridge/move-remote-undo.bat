@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0move-remote.ps1" undo
type "%~dp0move-remote.txt"
