$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example. Set your MySQL password, then run this script again.'
    exit 0
}
if (-not (Test-Path '.venv\Scripts\python.exe')) {
    throw 'Run setup.ps1 first.'
}
& '.venv\Scripts\python.exe' 'src\app.py'
