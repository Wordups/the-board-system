$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonExe = "C:\Python312\python.exe"
$logDir = Join-Path $repoRoot "backend\logs"
$logPath = Join-Path $logDir "local_refresh.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Add-Content -Path $logPath -Value "[$timestamp] $Message"
}

Write-Log "Starting local live board refresh."

Set-Location $repoRoot

# Sync with origin/main FIRST so the regenerate runs against current code,
# not whatever stale snapshot this checkout last had. Without this the
# script will happily regenerate JSON under outdated logic and try to
# push it back over freshly merged work, undoing PR landings.
git fetch origin main 2>&1 | Out-Null
$behind = (git rev-list --count HEAD..origin/main).Trim()
if ($behind -ne "0") {
    Write-Log "Local main is $behind commits behind origin/main; pulling before regenerate."
    git pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) {
        Write-Log "git pull --ff-only failed (likely uncommitted local changes or diverged history). Aborting refresh — resolve manually."
        exit 1
    }
}

& $pythonExe "backend\scripts\run_all.py"
Write-Log "Board rebuild finished."

git add `
    backend/data_final/picks.json frontend/data/picks.json data/picks.json `
    backend/data_final/mlb.json frontend/data/mlb.json data/mlb.json `
    backend/data_final/nba.json frontend/data/nba.json data/nba.json `
    backend/data_final/wnba.json frontend/data/wnba.json data/wnba.json `
    backend/data_final/soccer.json frontend/data/soccer.json data/soccer.json `
    backend/data_final/tennis.json frontend/data/tennis.json data/tennis.json

git diff --cached --quiet
if ($LASTEXITCODE -eq 0) {
    Write-Log "No board changes detected."
    exit 0
}

$commitMessage = "Refresh live boards"
git commit -m $commitMessage
if ($LASTEXITCODE -ne 0) {
    Write-Log "git commit FAILED (exit $LASTEXITCODE). Aborting before push."
    exit 1
}
Write-Log "Committed refreshed board data."

git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Log "git push origin main FAILED (exit $LASTEXITCODE). Local commit landed but origin is out of sync — resolve manually."
    exit 1
}
Write-Log "Pushed refreshed board data to origin/main."
