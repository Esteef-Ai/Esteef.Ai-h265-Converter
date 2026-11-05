<#
Worker-PC equivalent of supervisor.ps1: keeps worker.py running, restarting
it whenever it exits for any reason (crash, or the internal hang watchdog
deliberately exiting). Started by the desktop shortcut that
worker_install.ps1 creates (via worker_start.ps1).
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe = Join-Path $ScriptDir "venv\Scripts\python.exe"
$WorkerPy = Join-Path $ScriptDir "worker.py"
$LogDir = Join-Path $ScriptDir "logs"
$LogPath = Join-Path $LogDir "supervisor.log"
New-Item -ItemType Directory -Force $LogDir | Out-Null

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $msg"
    Write-Host $line
    Add-Content -Path $LogPath -Value $line
}

Log "Worker supervisor starting. Watching over $WorkerPy"

while ($true) {
    Log "Launching AIConverter worker..."
    $proc = Start-Process -FilePath $PythonExe -ArgumentList "`"$WorkerPy`"" -WorkingDirectory $ScriptDir -PassThru -NoNewWindow -Wait
    if ($proc.ExitCode -eq 0) {
        # Clean exit = another instance is already running (singleton lock).
        Log "Worker exited cleanly (code 0). Supervisor stopping."
        break
    }
    Log "Worker exited with code $($proc.ExitCode). Restarting in 10 seconds..."
    Start-Sleep -Seconds 10
}
