# Read-only check of the bridge on the target (saved login from install-remote): task account, process owner,
# firewall, folder rights, packages, log lines. Changes nothing. Output: remote-check.txt
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $dir "remote-check.txt"
$prefs = Get-Content (Join-Path $dir "remote-target.json") -Raw | ConvertFrom-Json
$cred = Import-Clixml (Join-Path $dir "remote-cred.xml")
$s = New-PSSession -ComputerName $prefs.target -Credential $cred
$r = Invoke-Command -Session $s {
    $d = "C:\StoeckieBridge"
    "=== task principal"; (Get-ScheduledTask -TaskName "Stoeckie Bridge").Principal | Format-List UserId, LogonType, RunLevel | Out-String
    "=== bridge process owner"
    Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object { $_.CommandLine -like "*stoeckie_bridge.py*" } |
        ForEach-Object { $o = Invoke-CimMethod -InputObject $_ -MethodName GetOwner; "$($_.ProcessId) $($o.Domain)\$($o.User)" }
    "=== firewall"
    Get-NetFirewallRule -Name "WINRM-HTTP-In-TCP*" -ErrorAction SilentlyContinue | ForEach-Object {
        "$($_.Name) enabled=$($_.Enabled) from=" + (($_ | Get-NetFirewallAddressFilter).RemoteAddress -join ",") }
    Get-NetFirewallRule -DisplayName "Stoeckie Bridge*" | ForEach-Object {
        "$($_.DisplayName) from=" + (($_ | Get-NetFirewallAddressFilter).RemoteAddress -join ",") }
    "=== acl"; (icacls $d) -join "`n"
    "=== packages"; (& "$d\venv\Scripts\python.exe" -m pip freeze 2>&1) -join "`n"
    "=== bridge.log (api, errors)"
    (Get-Content "$d\bridge.log" -Tail 300 -ErrorAction SilentlyContinue | Where-Object { $_ -match "\[api\]|ERROR|Traceback|listening" } |
        Select-Object -Last 25) -join "`n"
}
Remove-PSSession $s
$r | Out-File -Encoding utf8 $out
