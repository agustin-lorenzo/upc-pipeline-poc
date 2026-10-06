<#
.SYNOPSIS
  Runs the Teams bot locally and opens it in the Microsoft 365 Agents Playground.

.DESCRIPTION
  This is a local, Teams-like chat window, not real Teams. It needs no Azure, tunnel or admin.
  1. Starts the bot on http://localhost:3978 (anonymous mode, localhost only: local testing only).
  2. Starts the Playground (telemetry off) on http://localhost:56150 and opens it.
  3. Ctrl+C stops both.

  One-time setup:  winget install Microsoft.M365AgentsPlayground   (then re-run this script)
  Logs: output\bot.log, output\playground.log

.EXAMPLE
  .\start_teams_local.ps1
  .\start_teams_local.ps1 -NoBrowser
#>
param([switch]$NoBrowser)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
New-Item -ItemType Directory -Force output | Out-Null

if (-not (Test-Path $python)) { throw "No .venv yet. Run .\start.ps1 once first, or: python -m venv .venv; .venv\Scripts\pip install -r requirements-teams.txt" }
& $python -c "import microsoft_agents.hosting.aiohttp" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Teams dependencies missing. Run: .venv\Scripts\pip install -r requirements-teams.txt" }

$playground = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter agentsplayground.exe -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $playground) { throw "Agents Playground not installed. Run: winget install Microsoft.M365AgentsPlayground" }

$busy = Get-NetTCPConnection -State Listen -LocalPort 3978, 56150 -ErrorAction SilentlyContinue
if ($busy) { throw "Port 3978 or 56150 is already in use (an earlier run still going?). Stop it first." }

$env:TEAMS_BOT_ALLOW_ANONYMOUS = "true"
$procs = @()
function Stop-Tree($p) { if ($p -and -not $p.HasExited) { cmd /c "taskkill /T /F /PID $($p.Id) >nul 2>&1" } }

try {
    Write-Host "Starting the bot..."
    $bot = Start-Process $python -ArgumentList "-m", "teams_bot.app" -NoNewWindow -PassThru `
        -RedirectStandardOutput output\bot.log -RedirectStandardError output\bot.err.log
    $procs += $bot
    $deadline = (Get-Date).AddSeconds(45)
    while ($true) {
        try { if ((Invoke-WebRequest "http://localhost:3978/health" -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200) { break } } catch {}
        if ($bot.HasExited -or (Get-Date) -gt $deadline) { throw "The bot didn't start. See output\bot.err.log" }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "Starting the Playground..."
    $pg = Start-Process $playground.FullName -ArgumentList "-e", "http://localhost:3978/api/messages", "-c", "msteams", "--disable-telemetry" `
        -NoNewWindow -PassThru -RedirectStandardOutput output\playground.log -RedirectStandardError output\playground.err.log
    $procs += $pg
    $deadline = (Get-Date).AddSeconds(30)
    while ($true) {
        try { if ((Invoke-WebRequest "http://localhost:56150" -UseBasicParsing -TimeoutSec 1).StatusCode -eq 200) { break } } catch {}
        if ($pg.HasExited -or (Get-Date) -gt $deadline) { throw "The Playground didn't start. See output\playground.err.log" }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "`nReady: http://localhost:56150  (bot on :3978). Press Ctrl+C to stop both."
    if (-not $NoBrowser) { Start-Process "http://localhost:56150" }
    while (-not $bot.HasExited -and -not $pg.HasExited) { Start-Sleep 1 }
    Write-Warning "A process exited on its own; shutting down. See the logs in output\."
}
finally {
    Write-Host "`nStopping..."
    $procs | ForEach-Object { Stop-Tree $_ }
    Write-Host "Stopped."
}
