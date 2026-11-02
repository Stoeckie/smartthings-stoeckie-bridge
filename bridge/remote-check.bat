@echo off
rem Read-only check of the bridge on the other pc (task account, firewall, packages, log). Output: remote-check.txt
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0remote-check.ps1"
