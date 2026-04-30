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

& $pythonExe "backend\scripts\run_all.py"
Write-Log "Board rebuild finished."

git add `
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
Write-Log "Committed refreshed board data."

git push origin main
Write-Log "Pushed refreshed board data to origin/main."
