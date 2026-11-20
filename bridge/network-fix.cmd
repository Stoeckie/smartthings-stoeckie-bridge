<# : cmd part, PowerShell reads this as a comment
@echo off
rem Stoeckie Bridge network fix: run on the bridge pc itself, double click, Windows asks for admin (UAC) once.
rem 1. the bridge firewall rules hold on every network profile (also Public), still only for the own subnet or the
rem    allowed addresses; 2. the SYSTEM task "Stoeckie Bridge network private" that puts the network back on Private.
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
powershell -NoProfile -ExecutionPolicy Bypass -Command "$f='%~f0'; Invoke-Expression ([IO.File]::ReadAllText($f))"
pause
exit /b
#>
$ErrorActionPreference = "Stop"
$log = Join-Path $env:ProgramData "StoeckieBridge-network-fix-log.txt"
Start-Transcript -Path $log -Force | Out-Null
try {
    # 1. firewall rules of the bridge: every profile, never open to any address
    $rules = @(Get-NetFirewallRule -DisplayName "Stoeckie Bridge*" -ErrorAction SilentlyContinue)
    if (-not $rules.Count) { Write-Host "Geen firewallregels 'Stoeckie Bridge' gevonden: draai eerst install-remote." }
    foreach ($r in $rules) {
        $addr = @(($r | Get-NetFirewallAddressFilter).RemoteAddress)
        if ($r.DisplayName -eq "Stoeckie Bridge" -and $addr -contains "Any") {
            Set-NetFirewallRule -Name $r.Name -RemoteAddress LocalSubnet
            $addr = @("LocalSubnet")
        }
        if ($addr -contains "Any") { Write-Host "Overgeslagen (open voor elk adres): $($r.DisplayName)"; continue }
        Set-NetFirewallRule -Name $r.Name -Profile Any
        Write-Host "Regel '$($r.DisplayName)': alle profielen, alleen van $($addr -join ', ')"
    }

    # PowerShell remoting: the Public variant gets the same address limit as the normal rule
    # (the names differ per Windows version: pick the rules by profile)
    $wr = @(Get-NetFirewallRule -Name "WINRM-HTTP-In-TCP*" -ErrorAction SilentlyContinue)
    $w = $wr | Where-Object { $_.Enabled -eq "True" -and "$($_.Profile)" -match "Private" } | Select-Object -First 1
    $p = $wr | Where-Object { "$($_.Profile)" -match "Public" } | Select-Object -First 1
    if ($w -and $p) {
        $wAddr = @(($w | Get-NetFirewallAddressFilter).RemoteAddress)
        if ($wAddr -contains "Any") { Write-Host "WinRM Public niet aangezet: de gewone regel is open voor elk adres" }
        else {
            Set-NetFirewallRule -Name $p.Name -Enabled True -RemoteAddress $wAddr
            Write-Host "WinRM ook op Public, alleen van $($wAddr -join ', ')"
        }
    }

    # 2. SYSTEM task that puts the networks of today back on Private (same as install-remote)
    $netNames = @(Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne "Disconnected" } |
        ForEach-Object { $_.Name }) | Where-Object { $_ -match '^[A-Za-z0-9 ._-]{1,64}$' }
    $netTask = "Stoeckie Bridge network private"
    if (@($netNames).Count) {
        $list = ($netNames | ForEach-Object { "'" + ($_ -replace ' \d+$', '') + "'" } | Select-Object -Unique) -join ","
        $cmd = "`$ok=@($list); Get-NetConnectionProfile | Where-Object { `$_.NetworkCategory -eq 'Public' -and `$ok -contains (`$_.Name -replace ' \d+`$','') } | ForEach-Object { Set-NetConnectionProfile -InterfaceIndex `$_.InterfaceIndex -NetworkCategory Private }"
        $nAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -Command `"$cmd`""
        $nTriggers = @((New-ScheduledTaskTrigger -AtStartup),
            (New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5)))
        $nPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $nSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 2) -MultipleInstances IgnoreNew
        Register-ScheduledTask -TaskName $netTask -Action $nAction -Trigger $nTriggers -Principal $nPrincipal `
            -Settings $nSettings -Force | Out-Null
        # LOCAL SERVICE (LS, the bridge) may read and start this one task, nothing more
        $svc = New-Object -ComObject Schedule.Service; $svc.Connect()
        $t = $svc.GetFolder("\").GetTask($netTask)
        $sd = $t.GetSecurityDescriptor(4)
        if ($sd -notmatch ';;;LS\)') { $t.SetSecurityDescriptor($sd + "(A;;GRGX;;;LS)", 0) }
        Write-Host "Taak '$netTask' gemaakt voor netwerk(en): $($netNames -join ', ')"
        Start-ScheduledTask -TaskName $netTask
    } else { Write-Host "Geen verbonden netwerk gevonden: taak niet gemaakt" }
    Write-Host ""
    Write-Host "Klaar. Log: $log"
} catch {
    Write-Host "FOUT: $($_.Exception.Message)"
} finally {
    Stop-Transcript | Out-Null
}
