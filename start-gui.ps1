$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Agent-S virtual environment was not found at $python"
}

$env:PYTHONIOENCODING = "utf-8"
Set-Location $projectRoot
& $python -m gui_agents.s3.gui_app
