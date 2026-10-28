@echo off
setlocal
if not exist "%~dp0.runtime\python\python.exe" (
  echo Run Start.cmd first.
  exit /b 1
)
"%~dp0.runtime\python\python.exe" "%~dp0scripts\run_checkout.py" --test %*
exit /b %ERRORLEVEL%
