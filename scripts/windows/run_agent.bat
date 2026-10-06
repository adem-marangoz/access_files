@echo off
rem Runs the agent in this console window (Ctrl+C to stop).
rem Extra arguments are passed on, e.g.:  run_agent.bat -v
setlocal
cd /d "%~dp0..\.."
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
"%PY%" agent.py run %*
endlocal
