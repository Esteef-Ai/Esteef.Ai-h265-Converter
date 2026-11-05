<#
Desktop "AIConverter Worker - Start" shortcut target: launches the worker
(under its auto-restart supervisor) in the background, then opens the
control page. If it's already running, just opens the control page.
#>
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$running = Get-CimInstance Win32_Process | Where-Object { $_.Name -eq "powershell.exe" -and $_.CommandLine -match "worker_supervisor\.ps1" }
if (-not $running) {
    Start-Process powershell.exe -WindowStyle Hidden -WorkingDirectory $ScriptDir `
        -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$ScriptDir\worker_supervisor.ps1`""
    Start-Sleep -Seconds 6
}
Start-Process "http://127.0.0.1:8766/"
