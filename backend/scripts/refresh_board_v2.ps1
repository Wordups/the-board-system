# refresh_board_v2.ps1
# Local Windows fallback. Replaces refresh_live_boards_local.ps1.
#
# Why this script exists separately from the GitHub Actions workflow:
#   The Actions workflow is the source of truth and runs hourly in cloud.
#   This script is the local fallback for when (a) Actions is paused, or
#   (b) you want to force a refresh from your machine before the next
#   scheduled run lands.
#
# What broke in v1 (the bug this fixes):
#   $ErrorActionPreference = "Stop" + `git fetch ... 2>&1` causes
#   PowerShell 5.1 to treat git's normal stderr progress output (e.g.
#   "remote: Counting objects: ...") as a TERMINATING error, which kills
#   the script before run_all.py, commit, or push can run. The fix is to
#   stop redirecting stderr into the success stream and to gate failure
#   on $LASTEXITCODE explicitly, the way git itself signals failure.

# DO NOT set $ErrorActionPreference = "Stop" globally. Git on PS 5.1
# writes progress to stderr and we will not let that terminate us.
$ErrorActionPreference = "Continue"

$repoRoot  = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$pythonExe = "C:\Python312\python.exe"
$logDir    = Join-Path $repoRoot "backend\logs"
$logPath   = Join-Path $logDir   "local_refresh.log"

if (-not (Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir -Force | Out-Null
}

function Write-Log {
    param([string]$Level, [string]$Message)
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line      = "[$timestamp] [$Level] $Message"
    Add-Content -Path $logPath -Value $line
    Write-Host $line
}

function Invoke-Git {
    # Wrapper that runs git, captures BOTH streams into the log, and
    # returns the real exit code without letting stderr writes fault the
    # script. This is the single change that fixes the v1 bug.
    param([Parameter(Mandatory)][string[]]$GitArgs)

    $argString = ($GitArgs -join " ")
    Write-Log "INFO" "git $argString"

    # 2>&1 here merges stderr into the pipeline as objects, NOT as PS
    # error records, because we are NOT using $ErrorActionPreference=Stop.
    # The merged output is harmless text we can log.
    $output = & git @GitArgs 2>&1
    $code   = $LASTEXITCODE

    foreach ($line in $output) {
        Write-Log "GIT" ([string]$line)
    }

    return [pscustomobject]@{
        ExitCode = $code
        Output   = $output
    }
}

function Exit-With {
    param([string]$Status, [int]$Code = 0)
    Write-Log "RESULT" $Status
    Write-Host  $Status
    exit $Code
}

Write-Log "INFO" "=== refresh_board_v2 start ==="
Set-Location $repoRoot

# --- 1. Sync with origin/main so we regenerate against current code. ---
$fetch = Invoke-Git @("fetch", "origin", "main")
if ($fetch.ExitCode -ne 0) {
    Write-Log "ERROR" "git fetch failed (exit $($fetch.ExitCode))."
    Exit-With "FAILED-PIPELINE" 1
}

$behindResult = Invoke-Git @("rev-list", "--count", "HEAD..origin/main")
if ($behindResult.ExitCode -ne 0) {
    Write-Log "ERROR" "git rev-list failed (exit $($behindResult.ExitCode))."
    Exit-With "FAILED-PIPELINE" 1
}
$behind = ($behindResult.Output | Select-Object -Last 1).ToString().Trim()

if ($behind -ne "0") {
    Write-Log "INFO" "Local main is $behind commits behind origin/main; pulling."
    $pull = Invoke-Git @("pull", "--ff-only", "origin", "main")
    if ($pull.ExitCode -ne 0) {
        Write-Log "ERROR" "git pull --ff-only failed. Resolve manually."
        Exit-With "FAILED-PIPELINE" 1
    }
}

# --- 2. Run the Python pipeline. ---
Write-Log "INFO" "Running run_all.py with $pythonExe"
& $pythonExe (Join-Path $repoRoot "backend\scripts\run_all.py")
$pyExit = $LASTEXITCODE
if ($pyExit -ne 0) {
    Write-Log "ERROR" "run_all.py failed (exit $pyExit)."
    Exit-With "FAILED-PIPELINE" 1
}
Write-Log "INFO" "Board rebuild finished."

# --- 3. Stage refreshed JSON. ---
# Use directory globs so newly added sports (e.g. nfl.json) are picked up
# without editing this script. Matches the GitHub Actions workflow.
$add = Invoke-Git @(
    "add",
    "backend/data_final/*.json",
    "frontend/data/*.json",
    "data/*.json"
)
if ($add.ExitCode -ne 0) {
    Write-Log "ERROR" "git add failed (exit $($add.ExitCode))."
    Exit-With "FAILED-PIPELINE" 1
}

# --- 4. No-op fast path. ---
$diffCheck = Invoke-Git @("diff", "--cached", "--quiet")
if ($diffCheck.ExitCode -eq 0) {
    Exit-With "NO-CHANGES" 0
}

# --- 5. Commit. ---
$commit = Invoke-Git @("commit", "-m", "Refresh live boards")
if ($commit.ExitCode -ne 0) {
    Write-Log "ERROR" "git commit failed (exit $($commit.ExitCode))."
    Exit-With "FAILED-PIPELINE" 1
}

# --- 6. Push. ---
$push = Invoke-Git @("push", "origin", "main")
if ($push.ExitCode -ne 0) {
    Write-Log "ERROR" "git push failed (exit $($push.ExitCode)). Local commit landed; origin out of sync."
    Exit-With "FAILED-PUSH" 1
}

$shaResult = Invoke-Git @("rev-parse", "--short", "HEAD")
$shortSha  = ($shaResult.Output | Select-Object -Last 1).ToString().Trim()
Exit-With "LANDED: $shortSha" 0
