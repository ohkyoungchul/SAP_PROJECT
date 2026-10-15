@echo off
rem Evening prep: refresh daily bars + next-day features (about 45 min)
cd /d "%~dp0"
".venv\Scripts\python.exe" -m autotrader.prep_daily
