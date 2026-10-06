<#
.SYNOPSIS
    First-time setup: creates .venv, installs dependencies and creates
    config.yaml / .env from the examples (existing files are kept).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\setup.ps1
#>
[CmdletBinding()]
param(
    # Python used to create the virtual environment.
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Push-Location $RepoRoot
try {
    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "Creating virtual environment in .venv ..."
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Could not create the virtual environment with '$Python'." }
    }
    Write-Host "Installing dependencies ..."
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed." }

    if (-not (Test-Path "config.yaml")) {
        Copy-Item "config.example.yaml" "config.yaml"
        Write-Host "Created config.yaml - edit the projects section."
    }
    if (-not (Test-Path ".env")) {
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env - put your bot token and chat ID there."
    }

    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Put TELEGRAM_BOT_TOKEN into .env (from @BotFather)."
    Write-Host "  2. Send any message to your bot, then run:  .venv\Scripts\python.exe agent.py chat-id"
    Write-Host "  3. Put TELEGRAM_CHAT_ID into .env and edit the projects in config.yaml."
    Write-Host "  4. Verify:  .venv\Scripts\python.exe agent.py check"
    Write-Host "  5. Run:     scripts\windows\run_agent.bat   (or install_startup_task.ps1)"
}
finally {
    Pop-Location
}
