# Move the bridge from the Windows pc in remote-target.json to Docker on another machine: part on the Windows side.
# Run from this folder on your own pc (same login as install-remote / update-remote: remote-target.json and
# remote-cred.xml next to this script). Needs move-pub.pem from the new machine here (python move_data.py keygen).
#
#   move-remote.bat          stop and switch off the bridge on the target, export its data encrypted to move.enc here
#   move-remote.bat export http://<docker host>:8790/move
#                            same, then send move.enc to the new machine (move_data.py receive waits for it there)
#   move-remote.bat undo     switch the bridge on the target on again (only if the new machine does not run it)
#
# Never run both bridges with the same data: the Electrolux login rotates its refresh token.
# Result in move-remote.txt; only file names and a hash are written, never contents.
param([string]$Mode = "export", [string]$SendTo = "")
$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $dir "move-remote.txt"
$remoteDir = "C:\StoeckieBridge"
$taskName = "Stoeckie Bridge"
try {
    $prefs = Get-Content (Join-Path $dir "remote-target.json") -Raw | ConvertFrom-Json
    $cred = Import-Clixml (Join-Path $dir "remote-cred.xml")
    $s = New-PSSession -ComputerName $prefs.target -Credential $cred
    try {
        if ($Mode -eq "undo") {
            $r = Invoke-Command -Session $s { param($t)
                Enable-ScheduledTask -TaskName $t | Out-Null
                Start-ScheduledTask -TaskName $t
                Start-Sleep 5
                "task: " + (Get-ScheduledTask -TaskName $t).State } -ArgumentList $taskName
            ("OK undo " + (Get-Date -Format s)), $r | Out-File -Encoding utf8 $out
            return
        }
        $pub = Join-Path $dir "move-pub.pem"
        if (-not (Test-Path $pub)) { throw "move-pub.pem missing here (make it on the new machine: python move_data.py keygen)" }
        Copy-Item (Join-Path $dir "move_data.py") -Destination "$remoteDir\move_data.py" -ToSession $s -Force
        Copy-Item $pub -Destination "$remoteDir\move-pub.pem" -ToSession $s -Force
        $r = Invoke-Command -Session $s { param($d, $t)
            Disable-ScheduledTask -TaskName $t | Out-Null
            Stop-ScheduledTask -TaskName $t
            # the task ends cmd.exe; make sure the python of the bridge is gone too
            Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
                Where-Object { $_.ExecutablePath -like "$d\*" -or $_.CommandLine -like "*stoeckie_bridge.py*" } |
                ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
            Start-Sleep 3
            $left = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like "*stoeckie_bridge.py*" })
            if ($left.Count) { throw "bridge still running" }
            "task: " + (Get-ScheduledTask -TaskName $t).State
            Push-Location $d
            try { & "$d\venv\Scripts\python.exe" move_data.py export move-pub.pem $d "$d\move.enc" 2>&1 | ForEach-Object { "$_" } }
            finally { Pop-Location }
            if ($LASTEXITCODE -ne 0) { throw "export failed" }
        } -ArgumentList $remoteDir, $taskName
        Copy-Item "$remoteDir\move.enc" -Destination (Join-Path $dir "move.enc") -FromSession $s -Force
        Invoke-Command -Session $s { param($d) Remove-Item "$d\move.enc", "$d\move-pub.pem" -Force } -ArgumentList $remoteDir
        $lines = @(("OK export " + (Get-Date -Format s)), $r, ("move.enc here: " + (Get-Item (Join-Path $dir "move.enc")).Length + " bytes"))
        if ($SendTo) {
            $resp = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $SendTo -InFile (Join-Path $dir "move.enc") `
                -ContentType "application/octet-stream" -TimeoutSec 120
            # Content is a byte array when the answer has no text content type (Windows PowerShell 5)
            $body = if ($resp.Content -is [byte[]]) { [Text.Encoding]::UTF8.GetString($resp.Content) } else { "$($resp.Content)" }
            $lines += "sent to ${SendTo}: $($resp.StatusCode) $($body.Trim())"
            Remove-Item (Join-Path $dir "move.enc") -Force
        }
        $lines | Out-File -Encoding utf8 $out
    } finally { Remove-PSSession $s }
} catch {
    ("FOUT " + (Get-Date -Format s) + " " + $_.Exception.Message) | Out-File -Encoding utf8 $out
}
