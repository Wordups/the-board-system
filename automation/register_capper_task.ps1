# Registers the Beat The Books capper watch as Windows Scheduled Tasks.
# Run once:  powershell -File automation\register_capper_task.ps1
# Remove:    schtasks /Delete /TN "CapperWatchPregame" /F
#            schtasks /Delete /TN "CapperWatchGrade" /F
#
# Pregame runs three times on a staggered, human-looking cadence rather than a
# tight poll - this reads a Discord *user* account, and frequent automated
# reads are what gets accounts flagged.
# Grade runs once, late, after the last slate is final.

$watch = Join-Path $PSScriptRoot "run_capper_watch.cmd"
$grade = Join-Path $PSScriptRoot "run_capper_grade.cmd"

schtasks /Create /F /TN "CapperWatchPregame" /SC DAILY /ST 11:37 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchPregameB" /SC DAILY /ST 16:12 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchPregameC" /SC DAILY /ST 18:48 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchGrade"   /SC DAILY /ST 04:23 /TR "`"$grade`""

Write-Host "Registered 4 tasks. Test now with: schtasks /Run /TN CapperWatchPregame"
Write-Host "Chrome must be running and signed in to Discord for the pregame pass."
