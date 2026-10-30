@echo off
rem Installs the Stoeckie Bridge on another Windows pc on the LAN and moves it there. See install-remote.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-remote.ps1" %*
