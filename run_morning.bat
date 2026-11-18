@echo off
rem Daily jobscout run for Task Scheduler. Reads Gmail read-only, never opens a browser,
rem never sends anything. Each run appends one line (plus any crash output) to data\morning.log.
cd /d "%~dp0"
if not exist data mkdir data
".venv\Scripts\python.exe" jobscout.py morning >> "data\morning.log" 2>&1
exit /b %ERRORLEVEL%
