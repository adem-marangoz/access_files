<#
.SYNOPSIS
    Packages the agent with PyInstaller (Phase 5):
      dist\tpms-agent.exe          no console window - for running in the background
      dist\tpms-agent-console.exe  console version - for check / chat-id / send / report

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\build_exe.ps1
#>
[CmdletBinding()]
param(
    [string]$Python
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
if (-not $Python) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) { $Python = $venvPython } else { $Python = "python" }
}

Push-Location $RepoRoot
try {
    & $Python -m pip install -r requirements-dev.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

    # watchdog picks its Windows backend at runtime; make sure it is bundled.
    $common = @(
        "--noconfirm", "--onefile",
        "--hidden-import", "watchdog.observers.read_directory_changes",
        "--hidden-import", "watchdog.observers.winapi",
        "agent.py"
    )
    & $Python -m PyInstaller --clean --noconsole --name tpms-agent @common
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (tpms-agent) failed." }
    & $Python -m PyInstaller --console --name tpms-agent-console @common
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller (tpms-agent-console) failed." }

    Copy-Item "config.example.yaml" "dist\config.example.yaml" -Force
    Copy-Item ".env.example" "dist\.env.example" -Force

    Write-Host ""
    Write-Host "Built:"
    Write-Host "  dist\tpms-agent.exe          (background, no window)"
    Write-Host "  dist\tpms-agent-console.exe  (CLI: check, chat-id, send, report)"
    Write-Host "Copy the dist folder anywhere, add config.yaml + .env next to the exe, then:"
    Write-Host "  dist\tpms-agent-console.exe check"
    Write-Host "  scripts\windows\install_startup_task.ps1 -Exe <folder>\tpms-agent.exe"
}
finally {
    Pop-Location
}
