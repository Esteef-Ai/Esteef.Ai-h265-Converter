# One-click stop for the main converter (run via "Stop AIConverter.bat"). The
# supervisor goes first so it can't relaunch the app. A file in progress goes
# back in the queue the next time AIConverter starts.
$Here = [regex]::Escape((Split-Path -Parent $MyInvocation.MyCommand.Path))
$procs = Get-CimInstance Win32_Process
$stopped = 0
foreach ($p in $procs | Where-Object { $_.Name -eq "powershell.exe" -and $_.CommandLine -match "$Here\\supervisor\.ps1" }) {
    Stop-Process -Id $p.ProcessId -Force
    $stopped++
}
foreach ($p in $procs | Where-Object { $_.Name -eq "python.exe" -and $_.CommandLine -match "$Here\\main\.py" }) {
    & taskkill.exe /F /T /PID $p.ProcessId 2>$null | Out-Null
    $stopped++
}
if ($stopped) { Write-Host "AIConverter stopped." } else { Write-Host "AIConverter wasn't running." }
Start-Sleep 3
