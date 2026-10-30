# Restart the Stoeckie Bridge task and show what it sees. Output also in restart-log.txt.
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Transcript -Path (Join-Path $dir "restart-log.txt") -Force | Out-Null
Stop-ScheduledTask -TaskName "Stoeckie Bridge" -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*stoeckie_bridge.py*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 2
Start-ScheduledTask -TaskName "Stoeckie Bridge"
$ok = $false
for ($i = 0; $i -lt 15 -and -not $ok; $i++) {
    Start-Sleep 1
    try { $info = Invoke-RestMethod "http://127.0.0.1:8787/stbridge/info" -TimeoutSec 2; $ok = $true } catch {}
}
if ($ok) {
    Write-Host "Bridge draait: id $($info.id), versie $($info.version)"
    $d = Invoke-RestMethod "http://127.0.0.1:8787/stbridge/devices" -TimeoutSec 5
    $d | ConvertTo-Json -Depth 6 | Out-File -Encoding utf8 (Join-Path $dir "devices.json")
    Write-Host "Apparaten: $(@($d.devices).Count) (zie devices.json)"
} else {
    Write-Host "Bridge start niet. Taakstatus:"
    Get-ScheduledTask -TaskName "Stoeckie Bridge" | Get-ScheduledTaskInfo | Format-List LastRunTime, LastTaskResult | Out-String | Write-Host
    Write-Host "Laatste regels bridge.log:"
    Get-Content (Join-Path $dir "bridge.log") -Tail 15 -ErrorAction SilentlyContinue | Write-Host
}
Stop-Transcript | Out-Null
