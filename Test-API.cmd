@echo off
setlocal
cd /d "%~dp0"
if not exist ".runtime\python\python.exe" (
  echo Run Start.cmd once to install the local Python runtime, then retry Test-API.cmd.
  exit /b 1
)
".runtime\python\python.exe" -m beta.api %*
set "API_RESULT=%ERRORLEVEL%"
pause
exit /b %API_RESULT%
