<#
Restarts AIConverter whenever it exits, for any reason -- a code crash, or
the internal watchdog (aiconverter/heartbeat.py) deliberately exiting after
detecting a hang. A plain PowerShell loop -- no elevation required.

Run this for real unattended operation. To have it
start automatically at login with no admin rights, drop a shortcut to it in
your Startup folder -- see install_startup.ps1.
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir "venv\Scripts\python.exe"
$MainPy = Join-Path $ScriptDir "main.py"
$LogPath = Join-Path $ScriptDir "logs\supervisor.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

Log "Supervisor starting. Watching over $MainPy"

while ($true) {
    Log "Launching AIConverter..."
    $proc = Start-Process -FilePath $PythonExe -ArgumentList "`"$MainPy`"" -WorkingDirectory $ScriptDir -PassThru -NoNewWindow -Wait
    Log "AIConverter exited with code $($proc.ExitCode). Restarting in 5 seconds..."
    Start-Sleep -Seconds 5
}
