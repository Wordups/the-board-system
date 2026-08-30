@echo off
REM Post-slate pass — grades pending ledger rows against actual outcomes and
REM updates the per-capper record.
cd /d "%~dp0.."
claude -p "Read and execute automation/capper_watch.md exactly, in grade mode." --allowedTools "Read,Write,Edit,Bash,WebFetch" >> "%~dp0capper_watch.log" 2>&1
