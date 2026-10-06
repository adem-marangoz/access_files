<#
.SYNOPSIS
    Stops the background agent and removes its scheduled task.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall_startup_task.ps1
#>
[CmdletBinding()]
param(
    [string]$TaskName = "TPMS PC Agent"
)

$ErrorActionPreference = "Stop"
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "No scheduled task named '$TaskName' found."
    exit 0
}
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "Removed scheduled task '$TaskName'."
