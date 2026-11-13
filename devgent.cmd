@echo off
rem The agent workflow's command line for Windows. Run `devgent` for help.
setlocal
set PYTHONDONTWRITEBYTECODE=1
set PYTHONPATH=%~dp0.github\hooks
python -m devgent %*
