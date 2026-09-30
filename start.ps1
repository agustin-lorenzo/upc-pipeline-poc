<#
.SYNOPSIS
  Starts the mock services and the ADK chat UI with one command, and stops both on exit.

.DESCRIPTION
  1. Creates .venv and installs requirements-agent.txt if .venv doesn't exist yet.
  2. Checks that ports 8000-8003 are free (pass -Kill to stop whatever holds them).
  3. Starts the mock services (environment :8001, FCC :8002, ALS :8003) and waits for them.
  4. Starts `adk web` on :8000 and opens it in your browser. Pick "upc_agent".
  5. Ctrl+C (or closing the window) stops everything.

  Logs go to output\services.log and output\adk.log.

.EXAMPLE
  .\start.ps1
  .\start.ps1 -Kill        # free the ports first if an earlier run is still going
  .\start.ps1 -NoBrowser
#>
param(
    [switch]$Kill,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$adk = Join-Path $PSScriptRoot ".venv\Scripts\adk.exe"
$ports = 8000, 8001, 8002, 8003
New-Item -ItemType Directory -Force output | Out-Null

# --- 1. Environment ----------------------------------------------------------
if (-not (Test-Path $venvPython)) {
    Write-Host "Creating .venv and installing dependencies (first run only)..."
    python -m venv .venv
    & $venvPython -m pip install -q -r requirements-agent.txt
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
}
if (-not (Test-Path "upc_agent\.env")) {
    Write-Warning "upc_agent\.env not found. The agent needs a Gemini key: copy upc_agent\.env.example to upc_agent\.env and fill it in."
}

# --- 2. Ports ----------------------------------------------------------------
$busy = Get-NetTCPConnection -State Listen -LocalPort $ports -ErrorAction SilentlyContinue
if ($busy) {
    $owners = $busy | ForEach-Object {
        $p = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
        "  port $($_.LocalPort): $($p.ProcessName) (PID $($_.OwningProcess))"
    } | Sort-Object -Unique
    if (-not $Kill) {
        Write-Host "These ports are already in use:`n$($owners -join "`n")"
        Write-Host "Stop them yourself, or re-run with -Kill to stop those processes."
        exit 1
    }
    Write-Host "Stopping processes on ports $($ports -join ', ')..."
    $busy.OwningProcess | Sort-Object -Unique | ForEach-Object { cmd /c "taskkill /T /F /PID $_ >nul 2>&1" }
    Start-Sleep 1
}

# --- 3/4. Start everything, stop everything on the way out -------------------
$procs = @()
function Stop-Tree($p) { if ($p -and -not $p.HasExited) { cmd /c "taskkill /T /F /PID $($p.Id) >nul 2>&1" } }

try {
    # run_all.py regenerates the dummy data, then starts the three mock services.
    $services = Start-Process $venvPython -ArgumentList "run_all.py", "--services-only" -NoNewWindow -PassThru `
        -RedirectStandardOutput output\services.log -RedirectStandardError output\services.err.log
    $procs += $services

    Write-Host "Starting mock services..."
    $deadline = (Get-Date).AddSeconds(30)
    foreach ($port in 8001, 8002, 8003) {
        while ($true) {
            try { if ((Invoke-WebRequest "http://127.0.0.1:$port/health" -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200) { break } } catch {}
            if ($services.HasExited -or (Get-Date) -gt $deadline) { throw "Mock service on :$port didn't start. See output\services.log" }
            Start-Sleep -Milliseconds 300
        }
    }

    Write-Host "Starting ADK web UI..."
    $web = Start-Process $adk -ArgumentList "web", "--port", "8000" -NoNewWindow -PassThru `
        -RedirectStandardOutput output\adk.log -RedirectStandardError output\adk.err.log
    $procs += $web
    $deadline = (Get-Date).AddSeconds(45)
    while ($true) {
        try { if ((Invoke-WebRequest "http://127.0.0.1:8000" -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200) { break } } catch {}
        if ($web.HasExited -or (Get-Date) -gt $deadline) { throw "adk web didn't start. See output\adk.err.log" }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "`nReady: http://localhost:8000  (pick 'upc_agent'). Press Ctrl+C to stop everything."
    if (-not $NoBrowser) { Start-Process "http://localhost:8000" }

    while (-not $web.HasExited -and -not $services.HasExited) { Start-Sleep 1 }
    Write-Warning "A process exited on its own; shutting down. See the logs in output\."
}
finally {
    Write-Host "`nStopping..."
    $procs | ForEach-Object { Stop-Tree $_ }
    Write-Host "Stopped."
}
