<#
.SYNOPSIS
    Starts the SQL Server monitoring dashboard.

.DESCRIPTION
    Run in PowerShell from the project folder:
        .\run_app.ps1
    Then open http://dbmonitor.localhost in Edge or Chrome. Press Ctrl+C to stop.

    The dashboard listens on this PC only (loopback, port 80) - it is not
    reachable from other computers. Port and address: .streamlit\config.toml
#>

$ErrorActionPreference = 'Stop'

# Project virtual environment (kept outside OneDrive). Override with $env:CAPSTONE_VENV.
$venv = if ($env:CAPSTONE_VENV) { $env:CAPSTONE_VENV } else { Join-Path $env:USERPROFILE 'venvs\capstone' }
$python = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $python)) {
    Write-Host "Python virtual environment not found: $python" -ForegroundColor Red
    Write-Host 'Create it with:  python -m venv "$env:USERPROFILE\venvs\capstone"'
    Write-Host 'Then install:    & "$env:USERPROFILE\venvs\capstone\Scripts\python.exe" -m pip install -r requirements.txt'
    exit 1
}

Set-Location $PSScriptRoot

# Port 80 must be free (e.g. no IIS / other web server running).
if (Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue) {
    $owner = (Get-NetTCPConnection -LocalPort 80 -State Listen | Select-Object -First 1).OwningProcess
    Write-Host "Port 80 is already in use (process $owner) - is the dashboard already running?" -ForegroundColor Yellow
    Write-Host 'If it is, just open http://dbmonitor.localhost. Otherwise stop the other program first.'
    exit 1
}

Write-Host 'Starting dashboard at http://dbmonitor.localhost  (Ctrl+C to stop)' -ForegroundColor Cyan
& $python -m streamlit run app.py
