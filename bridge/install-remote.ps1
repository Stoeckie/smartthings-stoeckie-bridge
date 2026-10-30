# Install the Stoeckie Bridge on another Windows pc over the LAN (PowerShell remoting) and move it there.
# Run it on the pc where the bridge runs now, from the bridge folder: install-remote.bat
# The target needs PowerShell remoting once: run prep-remote.bat on the target as administrator.
#
# What it does:
#  - asks for the target IP and an administrator login of the target (nothing is stored)
#  - installs Python 3.12 on the target when missing, copies the bridge to C:\StoeckieBridge, venv + packages
#  - stops the bridge here and copies config.json (keys + bridge id, so SmartThings keeps the same devices)
#  - firewall rule 8787, scheduled task at startup as SYSTEM (runs without anyone logged on), starts it
#  - checks from here that the target answers on port 8787; then disables the bridge here (or restarts it on failure)
param([string]$Target)
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$remoteDir = "C:\StoeckieBridge"
$taskName = "Stoeckie Bridge"

$me = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $a = "-NoProfile -ExecutionPolicy Bypass -File `"$($MyInvocation.MyCommand.Path)`""
    if ($Target) { $a += " -Target $Target" }
    Start-Process powershell -Verb RunAs -ArgumentList $a
    exit
}
Start-Transcript -Path (Join-Path $dir "install-remote-log.txt") -Force | Out-Null

function Stop-LocalBridge {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
        Where-Object { $_.CommandLine -like "*stoeckie_bridge.py*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}
function Ask-Yes([string]$q, [bool]$default = $true) {
    $hint = if ($default) { "[J/n]" } else { "[j/N]" }
    $r = Read-Host "$q $hint"
    if (-not $r) { return $default }
    return $r -match "^[jJyY]"
}

$winrmWasStopped = $false
$winrmStartType = $null
$oldTrusted = $null
$session = $null
$localStopped = $false
try {
    # ---- target and login
    $saved = Join-Path $dir "remote-target.txt"
    if (-not $Target) {
        $def = if (Test-Path $saved) { (Get-Content $saved -Raw).Trim() } else { "" }
        $Target = Read-Host ("IP van de doel-pc" + $(if ($def) { " [$def]" } else { "" }))
        if (-not $Target) { $Target = $def }
    }
    if ($Target -notmatch '^\d{1,3}(\.\d{1,3}){3}$') { throw "Geen geldig IP adres: '$Target'" }
    Set-Content -Path $saved -Value $Target

    Write-Host ""
    Write-Host "Log in met een beheerder van $Target."
    Write-Host "Gebruiker: PCNAAM\gebruikersnaam, of het e-mailadres van het Microsoft account (wachtwoord, geen pincode)."
    $cred = Get-Credential -Message "Beheerder op $Target"
    if (-not $cred) { throw "Geen inloggegevens opgegeven." }

    # ---- WinRM client on this pc (temporarily trust the target IP)
    # (the WinRM service is often disabled on laptops: enable it for this run, restore it afterwards)
    $svc = Get-Service WinRM
    if ($svc.Status -ne "Running") {
        $winrmWasStopped = $true
        $winrmStartType = $svc.StartType
        if ($svc.StartType -eq "Disabled") { Set-Service WinRM -StartupType Manual }
        try { Start-Service WinRM } catch {
            $d = Get-CimInstance Win32_Service -Filter "Name='WinRM'"
            throw "WinRM start niet op deze pc (opstarttype $($d.StartMode), status $($d.State)): $($_.Exception.Message)"
        }
    }
    $oldTrusted = (Get-Item WSMan:\localhost\Client\TrustedHosts).Value
    if ($oldTrusted -ne "*" -and (($oldTrusted -split ",") -notcontains $Target)) {
        $new = if ($oldTrusted) { "$oldTrusted,$Target" } else { $Target }
        Set-Item WSMan:\localhost\Client\TrustedHosts -Value $new -Force
    }

    Write-Host "Verbinden met $Target..."
    try {
        $session = New-PSSession -ComputerName $Target -Credential $cred -ErrorAction Stop
    } catch {
        Write-Host ""
        Write-Host "Geen verbinding met $Target : $($_.Exception.Message)"
        Write-Host "1. Kopieer prep-remote.bat naar $Target, rechtsklik > Als administrator uitvoeren."
        Write-Host "2. Controleer gebruikersnaam en wachtwoord (beheerder van $Target)."
        Write-Host "3. Start install-remote.bat opnieuw."
        return
    }
    $info = Invoke-Command -Session $session {
        $p = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        [pscustomobject]@{ Admin = $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
                           Name = $env:COMPUTERNAME; Arch = $env:PROCESSOR_ARCHITECTURE
                           HasConfig = (Test-Path "C:\StoeckieBridge\config.json") }
    }
    if (-not $info.Admin) { throw "De gebruiker is geen beheerder op $Target." }
    Write-Host "Verbonden met $($info.Name)."

    # ---- Python on the target
    Write-Host "Python controleren op $Target..."
    $py = Invoke-Command -Session $session -ArgumentList $info.Arch {
        param($arch)
        $ErrorActionPreference = "Continue"
        $full = Join-Path $env:ProgramFiles "Python312\python.exe"
        if (Test-Path $full) { return $full }
        $probe = "import sys; print(sys.version_info[1] if sys.version_info[0] == 3 else 0); print(sys.executable)"
        foreach ($c in @("py", "python")) {
            try {
                if ($c -eq "py") { $r = & py -3 -c $probe 2>$null } else { $r = & python -c $probe 2>$null }
                if ($LASTEXITCODE -eq 0 -and @($r).Count -ge 2 -and [int]$r[0] -ge 10) { return "$($r[1])".Trim() }
            } catch {}
        }
        $kind = if ($arch -eq "ARM64") { "arm64" } else { "amd64" }
        $exe = Join-Path $env:TEMP "python-3.12.10-$kind.exe"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest "https://www.python.org/ftp/python/3.12.10/python-3.12.10-$kind.exe" -OutFile $exe -UseBasicParsing
        $p = Start-Process $exe -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0" -Wait -PassThru
        Remove-Item $exe -ErrorAction SilentlyContinue
        if ($p.ExitCode -ne 0 -or -not (Test-Path $full)) { throw "Python installatie mislukt (code $($p.ExitCode))" }
        return $full
    }
    Write-Host "Python: $py"

    # ---- code + packages (the bridge here keeps running meanwhile)
    Invoke-Command -Session $session -ArgumentList $remoteDir { param($d) New-Item -ItemType Directory -Force -Path $d | Out-Null }
    $files = @("stoeckie_bridge.py", "requirements.txt", "config.example.json", "restart.ps1", "restart.bat", "setup.bat", "check.bat") |
        ForEach-Object { Join-Path $dir $_ } | Where-Object { Test-Path $_ }
    Copy-Item -Path $files -Destination $remoteDir -ToSession $session -Force
    Write-Host "Packages installeren op $Target (paar minuten)..."
    Invoke-Command -Session $session -ArgumentList $remoteDir, $py {
        param($d, $py)
        $ErrorActionPreference = "Continue"
        $vpy = "$d\venv\Scripts\python.exe"
        if (-not (Test-Path $vpy)) { & $py -m venv "$d\venv" 2>&1 | Out-Null }
        if (-not (Test-Path $vpy)) { throw "venv aanmaken mislukt" }
        & $vpy -m pip install --upgrade pip --quiet 2>&1 | Out-Null
        $o = & $vpy -m pip install --upgrade -r "$d\requirements.txt" --quiet 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0) { throw "pip install mislukt: $o" }
    }

    # ---- options
    $myIp = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
             Select-Object -First 1).IPv4Address.IPAddress
    $allow = ""
    if ($myIp -and (Ask-Yes "Setup pagina van de bridge ook vanaf deze pc ($myIp) kunnen openen?")) { $allow = "$myIp/32" }
    $noSleep = Ask-Yes "Slaapstand uitzetten op $Target (nodig voor een bridge die altijd draait)?"
    $copyConfig = $true
    if ($info.HasConfig) {
        $copyConfig = Ask-Yes "Op $Target staat al een config.json. Overschrijven met die van deze pc?" $false
    }

    # ---- move: stop here, copy config.json, start there
    $localTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    $localId = $null
    if ($copyConfig) {
        if (-not (Test-Path "$dir\config.json")) { throw "Geen config.json in $dir" }
        Write-Host "Bridge op deze pc stoppen en config.json kopieren..."
        Stop-LocalBridge
        $localStopped = $true
        Start-Sleep 2
        $localId = (Get-Content "$dir\config.json" -Raw | ConvertFrom-Json).bridge_id
        Copy-Item -Path "$dir\config.json" -Destination $remoteDir -ToSession $session -Force
    }

    Write-Host "Firewall, taak en bridge instellen op $Target..."
    Invoke-Command -Session $session -ArgumentList $remoteDir, $taskName, $allow, $noSleep {
        param($d, $taskName, $allow, $noSleep)
        if (-not (Test-Path "$d\config.json")) { Copy-Item "$d\config.example.json" "$d\config.json" }
        if (-not (Get-NetFirewallRule -DisplayName "Stoeckie Bridge" -ErrorAction SilentlyContinue)) {
            New-NetFirewallRule -DisplayName "Stoeckie Bridge" -Direction Inbound -Protocol TCP -LocalPort 8787 `
                -Action Allow -Profile Private,Domain | Out-Null
        }
        Get-NetConnectionProfile | Where-Object { $_.NetworkCategory -eq "Public" } |
            ForEach-Object { Set-NetConnectionProfile -InterfaceIndex $_.InterfaceIndex -NetworkCategory Private }
        if ($noSleep) { & powercfg /change standby-timeout-ac 0; & powercfg /change hibernate-timeout-ac 0 }
        $run = @("@echo off", "cd /d `"%~dp0`"")
        if ($allow) { $run += "set STBRIDGE_SETUP_ALLOW=$allow" }
        $run += "`"%~dp0venv\Scripts\python.exe`" `"%~dp0stoeckie_bridge.py`""
        Set-Content -Path "$d\run.cmd" -Value $run -Encoding ASCII
        $action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$d\run.cmd`"" -WorkingDirectory $d
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
            -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
        Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
            Where-Object { $_.CommandLine -like "*stoeckie_bridge.py*" } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-ScheduledTask -TaskName $taskName
    }

    # ---- check from here (over the LAN, like the hub)
    Write-Host "Controleren of $Target antwoordt op poort 8787..."
    $ok = $null
    for ($i = 0; $i -lt 30 -and -not $ok; $i++) {
        Start-Sleep 2
        try { $ok = Invoke-RestMethod "http://${Target}:8787/stbridge/info" -TimeoutSec 3 } catch {}
    }
    if (-not $ok) {
        Write-Host ""
        Write-Host "De bridge op $Target antwoordt niet over het netwerk. Laatste regels van bridge.log daar:"
        Invoke-Command -Session $session -ArgumentList $remoteDir { param($d) Get-Content "$d\bridge.log" -Tail 15 -ErrorAction SilentlyContinue } | Write-Host
        if ($localStopped -and $localTask) { Start-ScheduledTask -TaskName $taskName; Write-Host "Bridge op deze pc weer gestart." }
        return
    }
    $devices = @((Invoke-RestMethod "http://${Target}:8787/stbridge/devices" -TimeoutSec 5).devices)
    Write-Host ""
    Write-Host "Bridge draait op $Target : versie $($ok.version), id $($ok.id), $($devices.Count) apparaten."
    if ($localId -and $ok.id -ne $localId) { Write-Host "LET OP: ander bridge id dan hier ($localId)." }
    if ($localStopped -and $localTask) {
        Disable-ScheduledTask -TaskName $taskName | Out-Null
        Write-Host "Bridge op deze pc uitgezet (taak uitgeschakeld). De map hier kun je later weggooien."
    }
    if ($allow) { Write-Host "Setup pagina: http://${Target}:8787/setup" }
    Write-Host "Geef $Target een vaste DHCP reservering in UniFi."
} catch {
    Write-Host ""
    Write-Host "FOUT: $($_.Exception.Message)"
    if ($localStopped -and (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) {
        Start-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
        Write-Host "Bridge op deze pc weer gestart."
    }
} finally {
    if ($session) { Remove-PSSession $session }
    if ($null -ne $oldTrusted) { Set-Item WSMan:\localhost\Client\TrustedHosts -Value $oldTrusted -Force }
    if ($winrmWasStopped) {
        Stop-Service WinRM -ErrorAction SilentlyContinue
        if ($winrmStartType -eq "Disabled") { Set-Service WinRM -StartupType Disabled -ErrorAction SilentlyContinue }
    }
    Stop-Transcript | Out-Null
    Read-Host "Druk op Enter om te sluiten"
}
