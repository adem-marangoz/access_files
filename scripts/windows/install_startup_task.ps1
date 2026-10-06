<#
.SYNOPSIS
    Starts the PC agent automatically in the background when you log on to
    Windows (Task Scheduler). The task runs as your user, without a window,
    and is restarted if it stops unexpectedly.

.EXAMPLE
    # Python version (uses .venv\Scripts\pythonw.exe if present)
    powershell -ExecutionPolicy Bypass -File scripts\windows\install_startup_task.ps1

.EXAMPLE
    # Packaged version
    powershell -ExecutionPolicy Bypass -File scripts\windows\install_startup_task.ps1 -Exe C:\Tools\tpms-agent\tpms-agent.exe -Config C:\Tools\tpms-agent\config.yaml
#>
[CmdletBinding()]
param(
    # config.yaml to use (default: config.yaml in the repository root).
    [string]$Config,
    # Packaged tpms-agent.exe; if omitted, pythonw.exe + agent.py are used.
    [string]$Exe,
    # pythonw.exe to use (default: .venv\Scripts\pythonw.exe, then PATH).
    [string]$Python,
    [string]$TaskName = "TPMS PC Agent",
    # Do not start the task right away.
    [switch]$NoStart
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path

if (-not $Config) {
    if ($Exe) { $Config = Join-Path (Split-Path -Parent $Exe) "config.yaml" }
    else { $Config = Join-Path $RepoRoot "config.yaml" }
}
if (-not (Test-Path $Config)) { throw "Config file not found: $Config (copy config.example.yaml to config.yaml first)" }
$Config = (Resolve-Path $Config).Path

if ($Exe) {
    $Exe = (Resolve-Path $Exe).Path
    $Execute = $Exe
    $Arguments = "run --config `"$Config`""
    $WorkDir = Split-Path -Parent $Exe
}
else {
    if (-not $Python) {
        $venvPython = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"
        if (Test-Path $venvPython) {
            $Python = $venvPython
        }
        else {
            $cmd = Get-Command "pythonw.exe", "pyw.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
            if (-not $cmd) { throw "pythonw.exe not found. Run setup.ps1 first or pass -Python <path to pythonw.exe>." }
            $Python = $cmd.Source
        }
    }
    $Execute = (Resolve-Path $Python).Path
    $Arguments = "`"$(Join-Path $RepoRoot 'agent.py')`" run --config `"$Config`""
    $WorkDir = $RepoRoot
}

$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $Execute -Argument $Arguments -WorkingDirectory $WorkDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description "PC file monitoring and Telegram notification agent" -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName':"
Write-Host "  $Execute $Arguments"
if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "Started. You should receive a 'PC Agent started' message in Telegram."
}
Write-Host "Logs: see logging.file in $Config (default: logs\agent.log next to it)."
Write-Host "Remove with: scripts\windows\uninstall_startup_task.ps1"
