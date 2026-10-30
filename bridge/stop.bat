@echo off
rem Stops the Stoeckie Bridge on this pc and switches off its autostart (start again: restart.bat, autostart back: install.bat).
powershell -NoProfile -Command "Stop-ScheduledTask -TaskName 'Stoeckie Bridge' -ErrorAction SilentlyContinue; Disable-ScheduledTask -TaskName 'Stoeckie Bridge' -ErrorAction SilentlyContinue | Out-Null; Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*stoeckie_bridge.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Start-Sleep 1; try { Invoke-RestMethod http://127.0.0.1:8787/stbridge/info -TimeoutSec 2 | Out-Null; Write-Host 'Bridge draait nog.' } catch { Write-Host 'Bridge gestopt, autostart uit.' }"
pause
