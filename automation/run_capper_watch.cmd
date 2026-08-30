@echo off
REM Pregame pass — reads the configured Beat The Books channels, logs and grades
REM new picks, notifies only on a calibrated pick clearing the EV bar.
cd /d "%~dp0.."
claude -p "Read and execute automation/capper_watch.md exactly, in pregame mode. Read only - never post in Discord and never place a bet." --allowedTools "mcp__claude-in-chrome__*,Read,Write,Edit,Bash,WebFetch,PushNotification" >> "%~dp0capper_watch.log" 2>&1
