@echo off
"%~dp0.runtime\python\python.exe" "%~dp0launcher.py" --stop
if errorlevel 1 pause
