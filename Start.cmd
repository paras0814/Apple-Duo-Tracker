@echo off
setlocal
set "DUO_PROJECT_ROOT=%~dp0"
if not exist "%~dp0.runtime\python\python.exe" (
  powershell.exe -NoProfile -Command "$ErrorActionPreference='Stop'; $r=Join-Path $env:DUO_PROJECT_ROOT '.runtime'; New-Item -ItemType Directory -Path $r -Force | Out-Null; $a=Join-Path $r 'python.zip'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.14.0/python-3.14.0-embed-amd64.zip' -OutFile $a; if ((Get-FileHash -LiteralPath $a -Algorithm SHA256).Hash -ne '8D4D3590C10449D78AA4375F534E6D5F3027D67FDC362DD1A882279DB6F90FDF') { throw 'Python checksum mismatch' }; Expand-Archive -LiteralPath $a -DestinationPath (Join-Path $r 'python') -Force"
  if errorlevel 1 exit /b 1
)
"%~dp0.runtime\python\python.exe" "%~dp0launcher.py" %*
if errorlevel 1 pause
