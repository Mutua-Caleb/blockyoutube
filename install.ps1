# Installs BlockYouTube as a Scheduled Task that runs at boot under SYSTEM.
# Run from an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# Assumes python.exe is on PATH for the SYSTEM account. If Python was installed
# "for all users" with "Add to PATH" checked this is true. Otherwise pass -PythonExe.

[CmdletBinding()]
param(
    [string]$TaskName = "BlockYouTube",
    [string]$PythonExe = "python.exe",
    [string]$InstallDir = "$env:ProgramData\BlockYouTube"
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

$action = New-ScheduledTaskAction -Execute $PythonExe -Argument "`"$blockerPath`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "Blocks configured websites; polls a remote JSON for unlock state." | Out-Null

Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started task '$TaskName'."
Write-Host "Logs: $InstallDir\blocker.log"
