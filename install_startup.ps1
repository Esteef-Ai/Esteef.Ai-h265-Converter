<#
One-time setup: creates a shortcut in your (current user's) Startup folder
so AIConverter's supervisor starts automatically at every login -- with NO
administrator rights required. This is the per-user Startup folder
(%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup), not the
all-users one, which is exactly why this doesn't need elevation.

Run once: powershell -ExecutionPolicy Bypass -File install_startup.ps1
#>

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SupervisorScript = Join-Path $ScriptDir "supervisor.ps1"
$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "AIConverter.lnk"

$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "powershell.exe"
$Shortcut.Arguments = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SupervisorScript`""
$Shortcut.WorkingDirectory = $ScriptDir
$Shortcut.Description = "AIConverter supervisor (auto-restart on crash/hang)"
$Shortcut.Save()

Write-Host "Installed startup shortcut at: $ShortcutPath"
Write-Host "AIConverter will now start automatically (via the supervisor, with auto-restart) at your next login."
Write-Host "To start it right now without waiting for a login, run: powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$SupervisorScript`""
