$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
if (-not (Test-Path '.venv\Scripts\python.exe')) { throw 'Run setup.ps1 first.' }
& '.venv\Scripts\python.exe' 'src\app.py'
