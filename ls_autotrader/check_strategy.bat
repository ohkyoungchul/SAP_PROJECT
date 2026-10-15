@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m autotrader --check-strategy
echo.
type data\strategy\latest_report.md 2>nul
pause
