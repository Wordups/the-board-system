# Registers the Beat The Books capper watch as Windows Scheduled Tasks.
# Run once:  powershell -File automation\register_capper_task.ps1
# Remove:    schtasks /Delete /TN "CapperWatchPregame" /F   (and B..E)
#            schtasks /Delete /TN "CapperWatchGrade" /F
#
# Reading is unrestricted - nothing is deployed into the server, this just
# drives Brian's own signed-in browser. Add or retime passes freely if picks
# are still being missed; five daily passes are set below.
# Grade runs once, late, after the last slate is final.

$watch = Join-Path $PSScriptRoot "run_capper_watch.cmd"
$grade = Join-Path $PSScriptRoot "run_capper_grade.cmd"

schtasks /Create /F /TN "CapperWatchPregame"  /SC DAILY /ST 09:41 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchPregameB" /SC DAILY /ST 12:26 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchPregameC" /SC DAILY /ST 15:14 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchPregameD" /SC DAILY /ST 17:52 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchPregameE" /SC DAILY /ST 20:38 /TR "`"$watch`""
schtasks /Create /F /TN "CapperWatchGrade"   /SC DAILY /ST 04:23 /TR "`"$grade`""

Write-Host "Registered 6 tasks. Test now with: schtasks /Run /TN CapperWatchPregame"
Write-Host "Chrome must be running and signed in to Discord for the pregame pass."
