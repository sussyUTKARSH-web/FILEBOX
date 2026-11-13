$ErrorActionPreference = 'Stop'
$Project = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Project
$Cloudflared = Join-Path $Project 'tools\cloudflared.exe'
if (-not (Test-Path -LiteralPath $Cloudflared)) { throw 'cloudflared.exe is missing from tools.\nDownload it from Cloudflare before running this script.' }
Write-Host 'Forwarding Cloudflare Tunnel traffic to the local FileBox origin at http://127.0.0.1:80'
Write-Host 'Start run.ps1 first. This Quick Tunnel URL is temporary and only works while this window stays open.'
& $Cloudflared tunnel --url 'http://127.0.0.1:80'
