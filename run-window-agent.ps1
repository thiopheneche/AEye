param(
    [Parameter(Mandatory = $true)]
    [string]$WindowTitle,

    [string]$Task,

    [string]$MainModel = "gpt-5.4",

    [string]$GroundingModel = "bytedance/ui-tars-1.5-7b"
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$agentExecutable = Join-Path $projectRoot ".venv\Scripts\agent_s.exe"

function Get-ConfiguredSecret([string]$Name) {
    foreach ($scope in @("Process", "User", "Machine")) {
        $value = [Environment]::GetEnvironmentVariable($Name, $scope)
        if ($value) {
            return $value
        }
    }
    throw "Required environment variable '$Name' was not found."
}

if (-not (Test-Path -LiteralPath $agentExecutable)) {
    throw "Agent-S virtual environment was not found at $agentExecutable"
}

# Agent-S's OpenAI-compatible adapter reads OPENAI_API_KEY. Keep the original
# user-level secret name and expose it only to this child process.
$env:OPENAI_API_KEY = Get-ConfiguredSecret "fyx_api_key"
$env:OPENROUTER_API_KEY = Get-ConfiguredSecret "OPENROUTER_API_KEY"
$env:PYTHONIOENCODING = "utf-8"

$agentArguments = @(
    "--provider", "openai",
    "--model", $MainModel,
    "--model_url", "https://ai.markfan.dpdns.org/v1",
    "--ground_provider", "open_router",
    "--ground_url", "https://openrouter.ai/api/v1",
    "--ground_model", $GroundingModel,
    "--grounding_width", "1920",
    "--grounding_height", "1080",
    "--window_title", $WindowTitle
)

if ($Task) {
    $agentArguments += @("--task", $Task)
}

Push-Location $projectRoot
try {
    & $agentExecutable @agentArguments
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
