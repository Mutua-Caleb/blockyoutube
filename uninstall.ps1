# Removes the BlockYouTube scheduled task and cleans hosts-file entries.
# Run from an elevated PowerShell:  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

[CmdletBinding()]
param(
    [string[]]$TaskNames = @("BlockYouTube", "BlockYouTube-Lock", "BlockYouTube-Sync"),
    [string]$InstallDir = "$env:ProgramData\BlockYouTube"
)

$ErrorActionPreference = "Stop"

$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$p  = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this script from an elevated (Administrator) PowerShell."
}

foreach ($TaskName in $TaskNames) {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'."
    }
}

# Clean BlockYouTube section from the hosts file.
$hosts = "$env:SystemRoot\System32\drivers\etc\hosts"
if (Test-Path $hosts) {
    $lines = Get-Content -Raw -Path $hosts -Encoding UTF8
    $cleaned = [System.Text.RegularExpressions.Regex]::Replace(
        $lines,
        '(?s)\r?\n?# BEGIN BLOCKYOUTUBE.*?# END BLOCKYOUTUBE\r?\n?',
        "`r`n"
    )
    if ($cleaned -ne $lines) {
        Set-Content -Path $hosts -Value $cleaned -Encoding UTF8 -NoNewline
        ipconfig /flushdns | Out-Null
        Write-Host "Cleaned hosts-file entries."
    }
}

if (Test-Path $InstallDir) {
    Write-Host "Install directory retained at $InstallDir (delete manually if desired)."
}
