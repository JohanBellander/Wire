# bootstrap-twitter.ps1
# One-shot PowerShell helper: load .env into the current process and run the
# X OAuth 2.0 PKCE bootstrap. Writes data/secrets/twitter-token.json.
#
# Usage from the Wire project root:
#   .\bootstrap-twitter.ps1

$ErrorActionPreference = 'Stop'

if (-not (Test-Path .env)) {
    Write-Error "No .env file in $(Get-Location). Copy .env.example and fill in values first."
    exit 1
}

# Parse .env line-by-line. Skips comments (#...) and blank lines. Trims whitespace.
Get-Content .env | ForEach-Object {
    if ($_ -match '^\s*([^#=][^=]*)=(.*)$') {
        $name  = $Matches[1].Trim()
        $value = $Matches[2].Trim()
        # Strip surrounding quotes if present
        if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -Path "env:$name" -Value $value
    }
}

# Sanity check the X-related vars are populated
foreach ($required in @('TWITTER_CLIENT_ID', 'TWITTER_CLIENT_SECRET')) {
    if (-not (Get-Item "env:$required" -ErrorAction SilentlyContinue).Value) {
        Write-Error "$required is empty in .env — fill it in before running this script."
        exit 1
    }
}

$env:WIRE_CONFIG_PATH = "$(Get-Location)\data\config.yaml"

Write-Host "Loaded .env. Running OAuth bootstrap..." -ForegroundColor Green
uv run python -m wire.scripts.twitter_auth
