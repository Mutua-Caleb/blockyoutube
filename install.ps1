# Installs BlockYouTube. Run from an elevated PowerShell:
#   powershell -ExecutionPolicy Bypass -File .\install.ps1
#
# What this does:
#   1. Resolves python.exe (so SYSTEM never has to use PATH).
#   2. Copies agent.py + config.json into C:\ProgramData\BlockYouTube\.
#   3. WRITES THE HOSTS-FILE BLOCK IMMEDIATELY (default-deny).
#   4. Registers two scheduled tasks running as SYSTEM:
#        - BlockYouTube-Lock:  one-shot at boot, --lock-only (no network).
#                              Default-deny while the daemon is starting up.
#        - BlockYouTube-Agent: long-lived push subscriber. At startup, with
#                              auto-restart on crash. Holds an SSE connection
#                              to ntfy and reacts to signed commands in <1s.

[CmdletBinding()]
param(
    [string]$LockTaskName  = "BlockYouTube-Lock",
    [string]$AgentTaskName = "BlockYouTube-Agent",
    [string]$PythonExe     = "",
    [string]$InstallDir    = "$env:ProgramData\BlockYouTube"
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

# Resolve python.exe. Prefer pythonw.exe for the daemon (no console window),
# but only if it sits next to a working python.exe.
if (-not $PythonExe) {
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) {
        $PythonExe = $cmd.Source
    } else {
        $candidates = @(
            "C:\Program Files\Python313\python.exe",
            "C:\Program Files\Python312\python.exe",
            "C:\Program Files\Python311\python.exe",
            "C:\Program Files\Python310\python.exe",
            "C:\Python313\python.exe",
            "C:\Python312\python.exe",
            "C:\Python311\python.exe"
        ) | Where-Object { Test-Path $_ }
        if ($candidates) { $PythonExe = $candidates[0] }
    }
}
if (-not $PythonExe -or -not (Test-Path $PythonExe)) {
    throw "Could not locate python.exe. Pass it explicitly: -PythonExe 'C:\Path\To\python.exe'"
}
$PythonwExe = Join-Path (Split-Path -Parent $PythonExe) "pythonw.exe"
if (-not (Test-Path $PythonwExe)) { $PythonwExe = $PythonExe }

Write-Host "Using Python:    $PythonExe"
Write-Host "Daemon Python:   $PythonwExe"

$scriptSource = $PSScriptRoot
if (-not $scriptSource) { $scriptSource = Split-Path -Parent $MyInvocation.MyCommand.Path }

Write-Host "Installing to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force -Path (Join-Path $scriptSource "agent.py") -Destination $InstallDir

$installedConfig = Join-Path $InstallDir "config.json"
$sourceConfig    = Join-Path $scriptSource "config.json"
$sourceKeygen    = Join-Path $scriptSource "keygen.py"
if (-not (Test-Path $installedConfig)) {
    if (Test-Path $sourceConfig) {
        Copy-Item -Force -Path $sourceConfig -Destination $installedConfig
    } else {
        # Generate a fresh skeleton via keygen.py (must be in the source dir)
        & $PythonExe $sourceKeygen --write $installedConfig
    }
    Write-Warning "Edit $installedConfig (cmd_topic / status_topic / secret) -- this same JSON goes into the phone PWA."
} else {
    Write-Host "Keeping existing config.json at $installedConfig"
}

$agentPath = Join-Path $InstallDir "agent.py"
$configPath = Join-Path $InstallDir "config.json"

# Default-deny: write the hosts block right now so YouTube is blocked even
# before any scheduled task runs. --lock-only does not touch the network.
Write-Host "Writing initial hosts-file block ..."
& $PythonExe $agentPath --config $configPath --lock-only
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Initial lock returned exit code $LASTEXITCODE; check $InstallDir\agent.log"
}

function Register-Task {
    param(
        [string]$Name,
        [string]$Exe,
        [string]$ArgString,
        $Trigger,
        $Settings
    )
    $action = New-ScheduledTaskAction -Execute $Exe -Argument $ArgString -WorkingDirectory $InstallDir
    $principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

    if (Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $Name -Confirm:$false
    }
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $Trigger `
        -Principal $principal `
        -Settings $Settings | Out-Null
    Write-Host "Registered task: $Name"
}

# Boot lock task: one-shot, --lock-only, no network. Finishes in seconds.
$lockTrigger = New-ScheduledTaskTrigger -AtStartup
$lockSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -MultipleInstances IgnoreNew

Register-Task `
    -Name $LockTaskName `
    -Exe $PythonExe `
    -ArgString ("`"{0}`" --config `"{1}`" --lock-only" -f $agentPath, $configPath) `
    -Trigger $lockTrigger `
    -Settings $lockSettings

# Long-lived agent task: at boot. If it dies, restart in 1 minute, indefinitely.
# ExecutionTimeLimit set to 0 means "run forever".
$agentTrigger = New-ScheduledTaskTrigger -AtStartup
$agentSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew

Register-Task `
    -Name $AgentTaskName `
    -Exe $PythonwExe `
    -ArgString ("`"{0}`" --config `"{1}`"" -f $agentPath, $configPath) `
    -Trigger $agentTrigger `
    -Settings $agentSettings

# Kick the agent off now so we don't have to reboot to test.
Start-ScheduledTask -TaskName $AgentTaskName

Write-Host ""
Write-Host "Installed."
Write-Host "  Boot lock:  $LockTaskName  (one-shot at startup, no network)"
Write-Host "  Agent:      $AgentTaskName  (long-lived push subscriber)"
Write-Host "  Logs:       $InstallDir\agent.log"
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Copy the {ntfy_base, cmd_topic, status_topic, secret} block from"
Write-Host "     $configPath into your Android PWA setup screen."
Write-Host "  2. From any machine: python cli.py --config <controller.json> status"
