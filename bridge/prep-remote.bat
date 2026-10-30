@echo off
rem Run once on the TARGET pc, as administrator (right click > Run as administrator).
rem Sets the network to Private and turns on PowerShell remoting, so install-remote.bat on another pc can install the bridge here.
net session >nul 2>&1
if errorlevel 1 (
  echo Rechtsklik op dit bestand en kies "Als administrator uitvoeren".
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq 'Public' } | ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }; Enable-PSRemoting -Force | Out-Null; Write-Host ('Klaar. Deze pc: ' + $env:COMPUTERNAME + '. Start nu install-remote.bat op de andere pc.')"
pause
