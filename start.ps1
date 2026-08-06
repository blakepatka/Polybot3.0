<#
.SYNOPSIS
  Run Polybot as a persistent background service with automatic restart.

.DESCRIPTION
  Supervises `run.py`. If the server exits for any reason — crash, unhandled
  exception, a killed console — it is restarted after a short backoff, so the
  engine keeps trading without someone watching it.

  Console output goes to logs\polybot.log; supervisor events go to
  logs\supervisor.log.

.EXAMPLE
  # Foreground (Ctrl+C to stop):
  .\start.ps1

.EXAMPLE
  # Detached, survives closing this terminal:
  .\start.ps1 -Detach

.EXAMPLE
  .\stop.ps1
#>
[CmdletBinding()]
param(
    [int]    $Port    = 8848,
    [string] $ServerHost = '127.0.0.1',
    [switch] $Detach,
    # Give up after this many restarts inside RetryWindowSeconds. Prevents a
    # hard config error from spinning in a tight crash loop forever.
    [int]    $MaxRetries         = 10,
    [int]    $RetryWindowSeconds = 300
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $root

$logDir = Join-Path $root 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }

$appLog       = Join-Path $logDir 'polybot.log'
$errLog       = Join-Path $logDir 'polybot.err.log'
$supLog       = Join-Path $logDir 'supervisor.log'
$pidFile      = Join-Path $logDir 'polybot.pid'
$childPidFile = Join-Path $logDir 'server.pid'

function Write-Sup([string] $Message) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $supLog -Value $line -Encoding utf8
    Write-Host $line
}

# ---- detach ---------------------------------------------------------------
if ($Detach) {
    $argList = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', "`"$($MyInvocation.MyCommand.Definition)`"",
        '-Port', $Port, '-ServerHost', $ServerHost
    )
    $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argList `
                          -WindowStyle Hidden -PassThru
    Write-Sup "detached supervisor started (pid $($proc.Id))"
    Write-Host ""
    Write-Host "  Polybot supervisor running detached (pid $($proc.Id))"
    Write-Host "  Dashboard : http://${ServerHost}:${Port}"
    Write-Host "  App log   : $appLog"
    Write-Host "  Stop with : .\stop.ps1"
    Write-Host ""
    return
}

# ---- already running? -----------------------------------------------------
$existing = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Sup "port $Port is already in use (pid $($existing[0].OwningProcess)); refusing to start a second engine"
    Write-Host "Already running. Use .\stop.ps1 first if you want to restart it."
    return
}

# ---- supervise ------------------------------------------------------------
Set-Content -Path $pidFile -Value $PID -Encoding utf8
Write-Sup "supervisor starting (pid $PID), target http://${ServerHost}:${Port}"

$restarts = @()

while ($true) {
    Write-Sup "launching run.py"
    $started = Get-Date

    # Start-Process rather than a direct call with `*>>` redirection.
    # uvicorn logs to stderr, and Windows PowerShell wraps a native command's
    # stderr into ErrorRecords — which, under $ErrorActionPreference='Stop',
    # turns ordinary startup output into a fatal error and crash-loops the
    # supervisor. Start-Process hands both streams straight to files and
    # returns a trustworthy exit code.
    try {
        $proc = Start-Process -FilePath 'py' `
            -ArgumentList @('run.py', '--host', $ServerHost, '--port', $Port) `
            -NoNewWindow -PassThru `
            -RedirectStandardOutput $appLog `
            -RedirectStandardError  $errLog
        Set-Content -Path $childPidFile -Value $proc.Id -Encoding utf8
        $proc.WaitForExit()
        $code = $proc.ExitCode
    } catch {
        $code = -1
        Write-Sup "launch threw: $($_.Exception.Message)"
    }

    $ranFor = [int]((Get-Date) - $started).TotalSeconds
    Write-Sup "run.py exited with code $code after ${ranFor}s"

    # Exit code 2 is the deliberate refusal to bind a non-loopback interface.
    # Restarting cannot fix an argument the operator chose.
    if ($code -eq 2) {
        Write-Sup "fatal configuration error; not restarting"
        break
    }

    $now = Get-Date
    $restarts = @($restarts | Where-Object { ($now - $_).TotalSeconds -lt $RetryWindowSeconds })
    $restarts += $now

    if ($restarts.Count -ge $MaxRetries) {
        Write-Sup "$($restarts.Count) restarts within ${RetryWindowSeconds}s - giving up. See $appLog"
        break
    }

    # Back off a little longer when it dies immediately, which usually means a
    # startup error rather than a transient fault.
    $delay = if ($ranFor -lt 10) { 10 } else { 3 }
    Write-Sup "restarting in ${delay}s (restart $($restarts.Count)/$MaxRetries)"
    Start-Sleep -Seconds $delay
}

Remove-Item $pidFile -ErrorAction SilentlyContinue
Write-Sup "supervisor exiting"
