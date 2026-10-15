@echo off
rem Scheduled auto trading (no pause). Logs: data\logs
cd /d "%~dp0"
".venv\Scripts\python.exe" -m autotrader
