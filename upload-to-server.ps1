# upload-to-server.ps1
#
# Uploads Wire's config + secrets to a server's bind-mount path so the Coolify
# container can read them. Idempotent — run again any time you change configs
# or rotate secrets.
#
# One-time prep on the server (so this script doesn't need sudo):
#   sudo mkdir -p /opt/wire-data
#   sudo chown $USER:$USER /opt/wire-data
#
# Usage from the Wire project root:
#   .\upload-to-server.ps1
#   .\upload-to-server.ps1 -SshTarget user@host -RemotePath /custom/path

param(
    [string]$SshTarget = "johan@gary",
    [string]$RemotePath = "/opt/wire-data"
)

$ErrorActionPreference = 'Stop'

# --- 1. Verify local files exist before touching the server -----------------

$required = @(
    "data\config.yaml",
    "data\repos.yaml",
    "data\secrets\github-app.pem",
    "data\secrets\twitter-token.json"
)
$missing = $required | Where-Object { -not (Test-Path $_) }
if ($missing) {
    Write-Host "Missing local files:" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Host ""
    Write-Host "Stage them before running this script:" -ForegroundColor Yellow
    Write-Host "  cp data\config.yaml.example data\config.yaml         # then edit"
    Write-Host "  cp data\repos.yaml.example  data\repos.yaml          # then edit"
    Write-Host "  # github-app.pem comes from the GitHub App download"
    Write-Host "  # twitter-token.json comes from .\bootstrap-twitter.ps1"
    exit 1
}

# --- 2. Show the upload plan ------------------------------------------------

Write-Host ""
Write-Host "Uploading to ${SshTarget}:${RemotePath}" -ForegroundColor Green
Write-Host "Files:" -ForegroundColor DarkGray
$required | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
Write-Host ""

# --- 3. Ensure remote dirs exist --------------------------------------------

& ssh $SshTarget "mkdir -p '$RemotePath/secrets'"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ssh failed. Verify the user owns ${RemotePath}:" -ForegroundColor Red
    Write-Host "  ssh $SshTarget" -ForegroundColor Yellow
    Write-Host "  sudo mkdir -p $RemotePath" -ForegroundColor Yellow
    Write-Host "  sudo chown `$USER:`$USER $RemotePath" -ForegroundColor Yellow
    exit 1
}

# --- 4. Upload --------------------------------------------------------------

& scp data\config.yaml                "${SshTarget}:${RemotePath}/config.yaml"
if ($LASTEXITCODE -ne 0) { Write-Error "scp config.yaml failed"; exit 1 }

& scp data\repos.yaml                 "${SshTarget}:${RemotePath}/repos.yaml"
if ($LASTEXITCODE -ne 0) { Write-Error "scp repos.yaml failed"; exit 1 }

& scp data\secrets\github-app.pem     "${SshTarget}:${RemotePath}/secrets/github-app.pem"
if ($LASTEXITCODE -ne 0) { Write-Error "scp github-app.pem failed"; exit 1 }

& scp data\secrets\twitter-token.json "${SshTarget}:${RemotePath}/secrets/twitter-token.json"
if ($LASTEXITCODE -ne 0) { Write-Error "scp twitter-token.json failed"; exit 1 }

# --- 5. Lock down secret permissions ---------------------------------------

& ssh $SshTarget @"
chmod 600 '$RemotePath/secrets/'*
chmod 644 '$RemotePath/config.yaml' '$RemotePath/repos.yaml'
"@

# --- 6. Verify --------------------------------------------------------------

Write-Host ""
Write-Host "Remote contents:" -ForegroundColor Green
& ssh $SshTarget "ls -la '$RemotePath' '$RemotePath/secrets'"

Write-Host ""
Write-Host "✅ Upload complete." -ForegroundColor Green
Write-Host ""
Write-Host "Next: in Coolify → your Wire app → Storages, configure:" -ForegroundColor Cyan
Write-Host "  Type:        Bind Mount" -ForegroundColor Cyan
Write-Host "  Source path: $RemotePath" -ForegroundColor Cyan
Write-Host "  Destination: /data" -ForegroundColor Cyan
Write-Host "Then hit Deploy." -ForegroundColor Cyan
