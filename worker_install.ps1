<#
Installs (or updates) an AIConverter worker on this PC. Normally run via the
one-line bootstrap served by the master (see README / dashboard):

  powershell -ExecutionPolicy Bypass -Command "iwr 'http://<master>:8765/worker/setup.ps1?key=<key>' -UseBasicParsing | iex"

Safe to re-run any time to update the worker to the master's current code:
it stops the running worker, replaces the code, keeps config and pause
state, and starts it again.

No admin rights needed: everything goes under C:\AIConverterWorker and the
current user's Desktop. Nothing starts at login.
#>
param(
    [Parameter(Mandatory = $true)][string]$MasterUrl,
    [Parameter(Mandatory = $true)][string]$Key,
    [Parameter(Mandatory = $true)][string]$PackageDir,
    [string]$InstallDir = "C:\AIConverterWorker",
    [switch]$NoShortcuts,  # testing only
    [switch]$NoStart       # testing only
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Invoke-WebRequest is extremely slow with the progress bar on

function Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }

Step "Installing AIConverter worker to $InstallDir (master: $MasterUrl)"
New-Item -ItemType Directory -Force $InstallDir, "$InstallDir\data", "$InstallDir\logs", "$InstallDir\work" | Out-Null

# --- 1. stop a running worker (update case) ---------------------------------
Step "Stopping any running worker"
$procs = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -eq "powershell.exe" -and $_.CommandLine -match "worker_supervisor\.ps1") -or
    ($_.Name -match "^pythonw?\.exe$" -and $_.CommandLine -match "worker\.py" -and $_.CommandLine -like "*$InstallDir*") -or
    ($_.Name -match "^(ffmpeg|ffprobe)\.exe$" -and $_.ExecutablePath -like "$InstallDir\*")
}
foreach ($p in $procs) {
    Write-Host "  stopping $($p.Name) (PID $($p.ProcessId))"
    & taskkill.exe /F /T /PID $p.ProcessId 2>$null | Out-Null
}

# --- 2. code ----------------------------------------------------------------
Step "Copying program files"
if (Test-Path "$InstallDir\aiconverter") { Remove-Item -Recurse -Force "$InstallDir\aiconverter" }
Copy-Item -Recurse -Force "$PackageDir\aiconverter" "$InstallDir\aiconverter"
foreach ($f in "worker.py", "worker_ctl.py", "worker_start.ps1", "worker_supervisor.ps1", "worker_install.ps1", "requirements.txt") {
    Copy-Item -Force "$PackageDir\$f" "$InstallDir\$f"
}

# --- 3. ffmpeg (same build as the master) ------------------------------------
$FfDir = "$InstallDir\ffmpeg"
if (Test-Path "$PackageDir\ffmpeg\ffmpeg.exe") {
    Step "Copying bundled ffmpeg"
    New-Item -ItemType Directory -Force $FfDir | Out-Null
    Copy-Item -Force "$PackageDir\ffmpeg\ffmpeg.exe", "$PackageDir\ffmpeg\ffprobe.exe" $FfDir
} elseif (-not (Test-Path "$FfDir\ffmpeg.exe") -or -not (Test-Path "$FfDir\ffprobe.exe")) {
    Step "Downloading ffmpeg from the master (~250 MB)"
    New-Item -ItemType Directory -Force $FfDir | Out-Null
    $zip = Join-Path $env:TEMP "aiconverter_ffmpeg.zip"
    Invoke-WebRequest "$MasterUrl/worker/ffmpeg.zip?key=$Key" -UseBasicParsing -OutFile $zip
    Expand-Archive -Force $zip $FfDir
    Remove-Item -Force $zip
} else {
    Step "ffmpeg already present"
}

# --- 4. Python ----------------------------------------------------------------
Step "Locating Python 3.10+"
function Find-Python {
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += , @("py", "-3") }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += , @("python") }
    foreach ($c in $candidates) {
        try {
            $exe = $c[0]
            $pre = @($c | Select-Object -Skip 1)
            $out = & $exe @pre -c "import sys; print(sys.version_info >= (3,10)); print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out -and $out[0] -eq "True" -and $out[1] -notmatch "WindowsApps") { return $out[1] }
        } catch {}
    }
    $guess = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue | Select-Object -Last 1
    if ($guess) { return $guess.FullName }
    return $null
}
$Python = Find-Python
if (-not $Python) {
    Step "Python not found -- installing Python 3.13 for this user via winget"
    winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + [Environment]::GetEnvironmentVariable("Path", "Machine")
    $Python = Find-Python
    if (-not $Python) { throw "Python 3.10+ is required and could not be installed automatically. Install it from python.org and re-run." }
}
Write-Host "  using $Python"

# --- 5. venv + packages --------------------------------------------------------
$VenvPy = "$InstallDir\venv\Scripts\python.exe"
if (-not (Test-Path $VenvPy)) {
    Step "Creating virtual environment"
    & $Python -m venv "$InstallDir\venv"
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
}
Step "Installing Python packages"
& $VenvPy -m pip install --disable-pip-version-check -q -r "$InstallDir\requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

# --- 6. config (kept across updates; master address/key refreshed) -------------
Step "Writing config.json"
$CfgPath = "$InstallDir\config.json"
if (Test-Path $CfgPath) {
    $cfg = Get-Content $CfgPath -Raw | ConvertFrom-Json
} else {
    $cfg = New-Object psobject
}
$set = @{
    role           = "worker"
    master_url     = $MasterUrl
    worker_api_key = $Key
    worker_name    = $env:COMPUTERNAME
    work_dir       = "$InstallDir\work"
    db_path        = "$InstallDir\data\aiconverter.db"
    log_dir        = "$InstallDir\logs"
    ffmpeg_bin     = "$FfDir\ffmpeg.exe"
    ffprobe_bin    = "$FfDir\ffprobe.exe"
}
$defaults = @{ process_priority = "below_normal"; worker_control_port = 8766; prevent_sleep_while_working = $true }
foreach ($k in $set.Keys) { $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $set[$k] -Force }
foreach ($k in $defaults.Keys) {
    if (-not ($cfg.PSObject.Properties.Name -contains $k)) { $cfg | Add-Member -NotePropertyName $k -NotePropertyValue $defaults[$k] }
}
[IO.File]::WriteAllText($CfgPath, ($cfg | ConvertTo-Json -Depth 5), (New-Object Text.UTF8Encoding $false))

# --- 7. sanity checks -----------------------------------------------------------
Step "Checking ffmpeg and master connection"
& "$FfDir\ffmpeg.exe" -hide_banner -version | Select-Object -First 1
$vmaf = & "$FfDir\ffmpeg.exe" -hide_banner -filters 2>$null | Select-String "libvmaf"
if (-not $vmaf) { throw "ffmpeg build is missing libvmaf" }
try {
    $body = @{ name = $env:COMPUTERNAME.ToUpper(); state = @{ mode = "installing" } } | ConvertTo-Json
    Invoke-RestMethod -Method Post -Uri "$MasterUrl/api/worker/heartbeat" -Headers @{ "X-Worker-Key" = $Key } -ContentType "application/json" -Body $body | Out-Null
    Write-Host "  master reachable and key accepted"
} catch {
    Write-Host "  WARNING: could not reach the master at $MasterUrl right now ($_)." -ForegroundColor Yellow
    Write-Host "  Install will finish anyway; the worker will connect once the master is reachable." -ForegroundColor Yellow
}

# --- 8. shortcuts ------------------------------------------------------------------
if ($NoShortcuts -or $NoStart) { Write-Host "Install finished (test mode: no shortcuts)."; return }
Step "Creating desktop shortcuts"
$Shell = New-Object -ComObject WScript.Shell

# Manual start only -- the worker never launches itself at login. Remove the
# Startup-folder shortcut older versions of this installer created.
$OldStartup = Join-Path ([Environment]::GetFolderPath("Startup")) "AIConverter Worker.lnk"
if (Test-Path $OldStartup) { Remove-Item -Force $OldStartup; Write-Host "  removed old start-at-login shortcut" }

$Desktop = [Environment]::GetFolderPath("Desktop")
$Pyw = "$InstallDir\venv\Scripts\pythonw.exe"

$l = $Shell.CreateShortcut("$Desktop\AIConverter Worker - Start.lnk")
$l.TargetPath = "powershell.exe"
$l.Arguments = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$InstallDir\worker_start.ps1`""
$l.WorkingDirectory = $InstallDir
$l.Description = "Start helping the AIConverter queue (opens the control page)"
$l.Save()

$shortcuts = @(
    @{ Name = "AIConverter Worker - Quit"; Arg = "quit"; Desc = "Hand the current file back to the queue and shut the worker down" },
    @{ Name = "AIConverter - Pause now"; Arg = "pause"; Desc = "Freeze the current encode instantly (keeps its place)" },
    @{ Name = "AIConverter - Resume"; Arg = "resume"; Desc = "Resume converting" },
    @{ Name = "AIConverter - Stop file and return to queue"; Arg = "stop"; Desc = "Abandon the current file (another PC can take it) and pause" }
)
foreach ($sc in $shortcuts) {
    $l = $Shell.CreateShortcut("$Desktop\$($sc.Name).lnk")
    $l.TargetPath = $Pyw
    $l.Arguments = "`"$InstallDir\worker_ctl.py`" $($sc.Arg)"
    $l.WorkingDirectory = $InstallDir
    $l.Description = $sc.Desc
    $l.Save()
}
Set-Content -Path "$Desktop\AIConverter Worker.url" -Value "[InternetShortcut]`r`nURL=http://127.0.0.1:8766/"

Write-Host ""
Write-Host "Installed. The worker is NOT running and will not start at login." -ForegroundColor Green
Write-Host "Double-click 'AIConverter Worker - Start' on the desktop when you want it to help."
Write-Host "'AIConverter Worker - Quit' hands back the current file and shuts it down."
