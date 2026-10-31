@echo off
rem Manual start of the agent with a visible console (for debugging).
rem The normal way is the scheduled task installed by install-agent.ps1.
cd /d "%~dp0"
python agent.py
pause
