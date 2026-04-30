# Make.ps1 — PowerShell equivalent of Makefile for Windows.
# Usage: pwsh ./Make.ps1 <target>
#   pwsh ./Make.ps1 test
#   pwsh ./Make.ps1 check
#   pwsh ./Make.ps1 inspect

param(
    [Parameter(Position=0)]
    [string]$Target = "help",

    [Parameter(Position=1, ValueFromRemainingArguments=$true)]
    [string[]]$ExtraArgs
)

$ErrorActionPreference = 'Stop'

$WIRE_PROJECT_PREFIX = "j13i32n8rrvzsxpydl404f6v"
$SSH_TARGET = if ($env:SSH_TARGET) { $env:SSH_TARGET } else { "johan@gary" }

function Help {
    Write-Host "Wire dev recipes (Make.ps1)" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  install       uv sync (runtime + dev deps)"
    Write-Host "  lint          Ruff lint"
    Write-Host "  format        Ruff format (in-place)"
    Write-Host "  format-check  Ruff format --check (no rewrite)"
    Write-Host "  test          Full pytest suite"
    Write-Host "  test-fast     Stop on first failure, short tracebacks"
    Write-Host "  typecheck     mypy (non-blocking)"
    Write-Host "  check         lint + format-check + test (what CI runs)"
    Write-Host "  verify        Full CI equivalent + typecheck"
    Write-Host "  inspect [hrs] wire.scripts.inspect against production (default 24h)"
    Write-Host "  logs          Tail production logs"
    Write-Host "  redeploy      git push + Coolify Deploy reminder"
    Write-Host "  clean         Remove caches"
}

function Install { uv sync }
function Lint { uv run ruff check src tests }
function Format { uv run ruff format src tests }
function FormatCheck { uv run ruff format --check src tests }
function Test { uv run pytest -q }
function TestFast { uv run pytest -q -x --tb=short }
function Typecheck {
    uv run mypy src
    # Non-blocking — return 0 even if mypy finds issues
    if ($LASTEXITCODE -ne 0) { Write-Host "(mypy found issues; non-blocking)" -ForegroundColor Yellow }
    $global:LASTEXITCODE = 0
}
function Check { Lint; FormatCheck; Test }
function Verify { Install; Lint; FormatCheck; Test; Typecheck }

function Inspect {
    $hours = if ($ExtraArgs -and $ExtraArgs.Count -gt 0) { $ExtraArgs[0] } else { "24" }
    & ssh $SSH_TARGET "docker exec `$(docker ps --filter name=$WIRE_PROJECT_PREFIX -q) python -m wire.scripts.inspect $hours"
}

function Logs {
    & ssh $SSH_TARGET "docker logs --tail 50 -f `$(docker ps --filter name=$WIRE_PROJECT_PREFIX -q)"
}

function Redeploy {
    git push
    Write-Host ""
    Write-Host "Now click Deploy in Coolify." -ForegroundColor Green
}

function Clean {
    foreach ($d in ".pytest_cache", ".ruff_cache", ".mypy_cache", "dist", "build", "htmlcov") {
        if (Test-Path $d) { Remove-Item -Recurse -Force $d }
    }
    Get-ChildItem -Path . -Filter __pycache__ -Recurse -Directory |
        ForEach-Object { Remove-Item -Recurse -Force $_.FullName }
    if (Test-Path .coverage) { Remove-Item .coverage }
}

switch ($Target) {
    "help"          { Help }
    "install"       { Install }
    "lint"          { Lint }
    "format"        { Format }
    "format-check"  { FormatCheck }
    "test"          { Test }
    "test-fast"     { TestFast }
    "typecheck"     { Typecheck }
    "check"         { Check }
    "verify"        { Verify }
    "inspect"       { Inspect }
    "logs"          { Logs }
    "redeploy"      { Redeploy }
    "clean"         { Clean }
    default {
        Write-Host "Unknown target: $Target" -ForegroundColor Red
        Write-Host ""
        Help
        exit 1
    }
}
