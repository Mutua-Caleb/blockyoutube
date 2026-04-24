# Installs BlockYouTube. Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# What this does:
#   1. Resolves the full path to python.exe (so SYSTEM never has to use PATH).
#   2. Copies blocker.py + config.json into C:\ProgramData\BlockYouTube\.
#   3. WRITES THE HOSTS-FILE BLOCK IMMEDIATELY so YouTube is blocked from
#      this moment, even before any task runs.
#   4. Registers two scheduled tasks running as SYSTEM:
#        - BlockYouTube-Lock:  one-shot at boot, --lock-only (no network).
#        - BlockYouTube-Sync:  one-shot every 5 minutes, polls the gist.
#      Both exit within seconds. There is no long-running process to die.

[CmdletBinding()]
param(
    [string]$LockTaskName = "BlockYouTube-Lock",
    [string]$SyncTaskName = "BlockYouTube-Sync",
    [string]$PythonExe = "",
    [string]$InstallDir = "$env:ProgramData\BlockYouTube",
    [int]$SyncIntervalMinutes = 5
)

$ErrorActionPreference = "Stop"

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $p  = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Run this script from an elevated (Administrator) PowerShell."
    }
}
Assert-Admin

# Resolve the full path to python.exe. Don't trust PATH for the SYSTEM account.
if (-not $PythonExe) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    } else {
        $candidates = @(
            "C:\Program Files\Python312\python.exe",
            "C:\Program Files\Python311\python.exe",
            "C:\Program Files\Python310\python.exe",
            "C:\Python312\python.exe",
            "C:\Python311\python.exe"
        ) | Where-Object { Test-Path $_ }
        if ($candidates) { $PythonExe = $candidates[0] }
    }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "Could not locate python.exe. Pass it explicitly: -PythonExe 'C:\Path\To\python.exe'"
}
Write-Host "Using Python at: $PythonExe"

$scriptSource = $PSScriptRoot
if (-not $scriptSource) { $scriptSource = Split-Path -Parent $MyInvocation.MyCommand.Path }

Write-Host "Installing to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force -Path (Join-Path $scriptSource "blocker.py") -Destination $InstallDir
if (-not (Test-Path (Join-Path $InstallDir "config.json"))) {
    Copy-Item -Force -Path (Join-Path $scriptSource "config.json") -Destination $InstallDir
    Write-Warning "Edit $InstallDir\config.json and set remote_url to your Gist raw URL."
} else {
    Write-Host "Keeping existing config.json at $InstallDir\config.json"
}

$blockerPath = Join-Path $InstallDir "blocker.py"

# Default-deny: write the hosts block right now so YouTube is blocked even
# before any scheduled task runs. We just invoke the script in --lock-only
# mode synchronously from this elevated shell.
Write-Host "Writing initial hosts-file block ..."
& $PythonExe $blockerPath --lock-only
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Initial lock returned exit code $LASTEXITCODE; check $InstallDir\blocker.log"
}

function Register-OneShotTask {
    param(
        [string]$Name,
        [string]$ArgString,
        $Trigger
    )
    $action = New-ScheduledTaskAction -Execute $PythonExe -Argument $ArgString -WorkingDirectory $InstallDir
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
        -MultipleInstances IgnoreNew

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $Trigger `
        -Principal $principal `
        -Settings $settings | Out-Null
    Write-Host "Registered task: $Name"
}

# Boot task — one-shot, --lock-only, no network call.
$bootTrigger = New-ScheduledTaskTrigger -AtStartup
Register-OneShotTask `
    -Name $LockTaskName `
    -ArgString ("`"{0}`" --lock-only" -f $blockerPath) `
    -Trigger $bootTrigger

# Sync task — runs every N minutes, indefinitely, starting now.
$syncTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(30) `
    -RepetitionInterval (New-TimeSpan -Minutes $SyncIntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))
Register-OneShotTask `
    -Name $SyncTaskName `
    -ArgString ("`"{0}`"" -f $blockerPath) `
    -Trigger $syncTrigger

Start-ScheduledTask -TaskName $SyncTaskName
Write-Host ""
Write-Host "Installed."
Write-Host "  Boot lock:  $LockTaskName  (one-shot at startup, no network)"
Write-Host "  Sync poll:  $SyncTaskName  (every $SyncIntervalMinutes minutes)"
Write-Host "  Logs:       $InstallDir\blocker.log"
