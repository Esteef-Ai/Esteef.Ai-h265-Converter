<#
First-time setup for the main PC. Run from this folder:

  powershell -ExecutionPolicy Bypass -File setup.ps1

Creates the Python environment and checks that ffmpeg can do the job. Safe to
re-run. No administrator rights needed.

To let other PCs help with the workload (see docs/MULTIPLE_MACHINES.md):

  powershell -ExecutionPolicy Bypass -File setup.ps1 -EnableWorkers
#>
param([switch]$EnableWorkers)
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Venv = Join-Path $ScriptDir "venv\Scripts\python.exe"

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }

function Find-Python {
    foreach ($c in @(@("py", "-3"), @("python"))) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        $pre = @($c | Select-Object -Skip 1)
        $ErrorActionPreference = "Continue"
        $out = & $c[0] @pre -c "import sys; print(sys.version_info >= (3,10)); print(sys.executable)" 2>$null
        $code = $LASTEXITCODE
        $ErrorActionPreference = "Stop"
        if ($code -eq 0 -and $out -and $out[0] -eq "True" -and $out[1] -notmatch "WindowsApps") { return $out[1] }
    }
    return $null
}

Step "Python"
if (-not (Test-Path $Venv)) {
    $Python = Find-Python
    if (-not $Python) { throw "Python 3.10 or newer is required. Install it from python.org and re-run." }
    Write-Host "  using $Python"
    & $Python -m venv (Join-Path $ScriptDir "venv")
    if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment." }
}
& $Venv -m pip install --disable-pip-version-check -q -r (Join-Path $ScriptDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed." }
Write-Host "  packages installed"

Step "ffmpeg"
foreach ($tool in "ffmpeg", "ffprobe") {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found on PATH. See 'What you need' in README.md."
    }
}
$ErrorActionPreference = "Continue"
$version = (& ffmpeg -hide_banner -version 2>$null | Select-Object -First 1)
$encoders = (& ffmpeg -hide_banner -encoders 2>$null | Out-String)
$filters = (& ffmpeg -hide_banner -filters 2>$null | Out-String)
$options = (& ffmpeg -hide_banner -h full 2>$null | Out-String)
$ErrorActionPreference = "Stop"
Write-Host "  $version"
if ($encoders -notmatch "libx265") { throw "This ffmpeg build has no libx265 encoder. See 'What you need' in README.md." }
if ($filters -notmatch "libvmaf") { throw "This ffmpeg build has no libvmaf filter. See 'What you need' in README.md." }
if ($options -notmatch "apply_cropping") { throw "This ffmpeg is too old: version 7.1 or newer is required." }
Write-Host "  libx265 and libvmaf available"

if ($EnableWorkers) {
    Step "Enabling worker PCs"
    $CfgPath = Join-Path $ScriptDir "config.json"
    if (Test-Path $CfgPath) { $cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json } else { $cfg = New-Object psobject }
    $key = $cfg.worker_api_key
    if (-not $key) { $key = -join ((1..32) | ForEach-Object { "{0:x}" -f (Get-Random -Maximum 16) }) }
    $port = $cfg.web_port
    if (-not $port) { $port = 8765 }
    $cfg | Add-Member -NotePropertyName worker_api_key -NotePropertyValue $key -Force
    $cfg | Add-Member -NotePropertyName web_host -NotePropertyValue "0.0.0.0" -Force
    [IO.File]::WriteAllText($CfgPath, ($cfg | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))

    $ip = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq "Up" } |
           Select-Object -First 1).IPv4Address.IPAddress
    if (-not $ip) { $ip = "<this-PC's-IP>" }
    Write-Host "  config.json updated. Restart AIConverter for this to take effect."
    Write-Host ""
    Write-Host "The dashboard is now reachable by anyone on your network, and it has no login." -ForegroundColor Yellow
    Write-Host "Only do this on a network you trust." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "1. Allow workers through this PC's firewall (run once, in an ADMINISTRATOR PowerShell):"
    Write-Host "   New-NetFirewallRule -DisplayName AIConverter -Direction Inbound -Protocol TCP -LocalPort $port -Action Allow -RemoteAddress LocalSubnet"
    Write-Host ""
    Write-Host "2. On each worker PC, in a normal PowerShell window:"
    Write-Host "   powershell -ExecutionPolicy Bypass -Command `"iwr 'http://${ip}:$port/worker/setup.ps1?key=$key' -UseBasicParsing | iex`""
    Write-Host ""
    Write-Host "Treat that command like a password: it contains this install's worker key."
}

Write-Host ""
Write-Host "Done. Double-click 'Start AIConverter.bat' to run it." -ForegroundColor Green
