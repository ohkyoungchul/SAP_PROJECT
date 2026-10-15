@echo off
rem Saturday: re-optimize on last 40 trading days, auto-apply data\strategy\strategy.json if criteria pass
cd /d "%~dp0"
"analysis\.venv-ana\Scripts\python.exe" analysis\weekly_update.py %*
