# Stoeckie Bridge installer for Windows.
# - installs Python 3.12 (per user, via winget) when missing
# - creates a venv next to this script and installs the dependencies
# - creates config.json from config.example.json
# - opens TCP 8787 in the Windows firewall (asks for admin once)
# - registers a scheduled task that starts the bridge hidden at logon and restarts it on failure
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $dir
$out = Join-Path $dir "install-log.txt"
Start-Transcript -Path $out -Force | Out-Null

function Find-Python {
    foreach ($c in @("py -3", "python")) {
        try {
            $v = & cmd /c "$c --version 2>&1"
            if ($LASTEXITCODE -eq 0 -and $v -match "Python 3\.(\d+)" -and [int]$Matches[1] -ge 10) { return $c }
        } catch {}
    }
    return $null
}

$py = Find-Python
if (-not $py) {
    Write-Host "Python niet gevonden, installeren via winget..."
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $py = Find-Python
    if (-not $py) { throw "Python installatie mislukt. Installeer Python 3.12 van python.org en start opnieuw." }
}
Write-Host "Python: $py"

if (-not (Test-Path "$dir\venv\Scripts\python.exe")) {
    & cmd /c "$py -m venv `"$dir\venv`""
}
& "$dir\venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& "$dir\venv\Scripts\python.exe" -m pip install --upgrade -r "$dir\requirements.txt" --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install mislukt" }

if (-not (Test-Path "$dir\config.json")) {
    Copy-Item "$dir\config.example.json" "$dir\config.json"
    Write-Host "config.json aangemaakt. Vul de Electrolux gegevens in."
}

# firewall: inbound TCP 8787 on private networks (needs admin)
$rule = Get-NetFirewallRule -DisplayName "Stoeckie Bridge" -ErrorAction SilentlyContinue
if (-not $rule) {
    Write-Host "Firewall regel toevoegen (Windows vraagt om toestemming)..."
    Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command "New-NetFirewallRule -DisplayName ''Stoeckie Bridge'' -Direction Inbound -Protocol TCP -LocalPort 8787 -Action Allow -Profile Private,Domain | Out-Null"'
}

# autostart: hidden at logon, restart on failure
$action = New-ScheduledTaskAction -Execute "$dir\venv\Scripts\pythonw.exe" -Argument "`"$dir\stoeckie_bridge.py`"" -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable
Register-ScheduledTask -TaskName "Stoeckie Bridge" -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
Write-Host "Taak 'Stoeckie Bridge' geregistreerd (start bij aanmelden)."

& powershell -NoProfile -ExecutionPolicy Bypass -File "$dir\restart.ps1"
$ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } | Select-Object -First 1).IPv4Address.IPAddress
$net = Get-NetConnectionProfile | Where-Object { $_.IPv4Connectivity -ne "Disconnected" } | Select-Object -First 1
if ($net -and $net.NetworkCategory -eq "Public") {
    Write-Host "LET OP: netwerk '$($net.Name)' staat op Openbaar. Zet het op Prive, anders kan de hub de bridge niet bereiken."
}
Write-Host "IP van deze pc: $ip (geef hem een DHCP reservering in UniFi)"
Start-Process "http://localhost:8787/setup"
Stop-Transcript | Out-Null
