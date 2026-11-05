# One-click start for the main converter on this PC (run via "Start AIConverter.bat").
# - Stops a worker accidentally started here (the worker is for the second PC only).
# - Starts the supervisor (which restarts AIConverter if it ever exits) unless
#   AIConverter is already running, clearing out a stuck supervisor first.
# - Opens the dashboard.
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Port = 8765
$Cfg = Join-Path $ScriptDir "config.json"
if (Test-Path $Cfg) {
    $saved = (Get-Content $Cfg -Raw | ConvertFrom-Json).web_port
    if ($saved) { $Port = $saved }
}
$Dashboard = "http://localhost:$Port/"
# Only ever touch processes started from this folder.
$Here = [regex]::Escape($ScriptDir)

function DashboardUp {
    try { return (Invoke-WebRequest $Dashboard -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch { return $false }
}

$shells = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'"
foreach ($p in $shells | Where-Object { $_.CommandLine -match "$Here\\worker_supervisor\.ps1" }) {
    Write-Host "Stopping the worker supervisor (the worker is for the second PC, not this one)"
    Stop-Process -Id $p.ProcessId -Force
}

if (DashboardUp) {
    Write-Host "AIConverter is already running."
} else {
    foreach ($p in $shells | Where-Object { $_.CommandLine -match "$Here\\supervisor\.ps1" }) {
        Write-Host "Stopping a stuck supervisor"
        Stop-Process -Id $p.ProcessId -Force
    }
    foreach ($p in Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
             Where-Object { $_.CommandLine -match "$Here\\(main|worker)\.py" }) {
        Stop-Process -Id $p.ProcessId -Force
    }
    Write-Host "Starting AIConverter..."
    $sup = Join-Path $ScriptDir "supervisor.ps1"
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$sup`""
    $ok = $false
    for ($i = 0; $i -lt 60 -and -not $ok; $i++) { Start-Sleep 2; $ok = DashboardUp }
    if ($ok) { Write-Host "AIConverter is running." }
    else { Write-Host "The dashboard did not come up within 2 minutes -- check logs\aiconverter.log" }
}

Start-Process $Dashboard
Start-Sleep 4
