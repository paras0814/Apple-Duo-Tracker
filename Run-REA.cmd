@echo off
setlocal
set /p REA_NODE_DIR=<"%~dp0.runtime\node-path.txt"
set "PATH=%REA_NODE_DIR%;%~dp0.runtime\git\cmd;%PATH%"
"%REA_NODE_DIR%\node.exe" "%~dp0rea\scripts\rea.mjs" %*
