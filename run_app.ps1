<#
.SYNOPSIS
    Starts the SQL Server monitoring dashboard.

.DESCRIPTION
    Run in PowerShell from the project folder:
        .\run_app.ps1
    Then open http://dbmonitor.localhost in Edge or Chrome. Press Ctrl+C to stop.

    It also starts the performance-history collector (tools\collect_history.py) in
    the background, and stops it again when the dashboard stops.

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

# Performance-history collector (Module 6): a snapshot of every server every 5 minutes,
# in the background. Its output goes to %USERPROFILE%\CapstoneMonitor\collector.log.
$collector = $null
$running = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*collect_history.py*' }
if ($running) {
    Write-Host "History collector already running (process $(@($running)[0].ProcessId))." -ForegroundColor DarkGray
} else {
    $logDir = Join-Path $env:USERPROFILE 'CapstoneMonitor'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $collector = Start-Process -FilePath $python -ArgumentList 'tools\collect_history.py' `
        -WorkingDirectory $PSScriptRoot -NoNewWindow -PassThru `
        -RedirectStandardOutput (Join-Path $logDir 'collector.log') `
        -RedirectStandardError (Join-Path $logDir 'collector-errors.log')
    Write-Host "History collector started (process $($collector.Id)) - log: $logDir\collector.log" -ForegroundColor DarkGray
}

Write-Host 'Starting dashboard at http://dbmonitor.localhost  (Ctrl+C to stop)' -ForegroundColor Cyan
try {
    # Port 80 on the loopback address only (not reachable from other computers).
    # Edge / Chrome resolve every *.localhost name to this PC by themselves, so
    # http://dbmonitor.localhost works without DNS, hosts-file or admin changes.
    & $python -m streamlit run app.py `
        --server.address localhost --server.port 80 `
        --browser.serverAddress dbmonitor.localhost --browser.serverPort 80
} finally {
    # Stop the collector this script started (one started elsewhere keeps running).
    if ($collector -and -not $collector.HasExited) {
        Stop-Process -Id $collector.Id -ErrorAction SilentlyContinue
        Write-Host 'History collector stopped.' -ForegroundColor DarkGray
    }
}
