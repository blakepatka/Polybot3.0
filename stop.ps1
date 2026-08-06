<#
.SYNOPSIS
  Stop the Polybot supervisor and its server.

.DESCRIPTION
  Stops the supervisor first so it does not immediately restart the server it
  is about to lose, then stops whatever is listening on the port.
#>
[CmdletBinding()]
param([int] $Port = 8848)

$root    = Split-Path -Parent $MyInvocation.MyCommand.Definition
$logDir  = Join-Path $root 'logs'
$pidFile = Join-Path $logDir 'polybot.pid'

# Supervisor first — otherwise it treats the dying server as a crash and
# relaunches it a few seconds later.
if (Test-Path $pidFile) {
    $supPid = (Get-Content $pidFile -Raw).Trim()
    if ($supPid) {
        try {
            Stop-Process -Id ([int]$supPid) -Force -ErrorAction Stop
            Write-Host "Stopped supervisor (pid $supPid)."
        } catch {
            Write-Host "Supervisor (pid $supPid) was not running."
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

$conns = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if (-not $conns) {
    Write-Host "Nothing listening on port $Port."
    return
}

foreach ($pid_ in ($conns.OwningProcess | Select-Object -Unique)) {
    try {
        Stop-Process -Id $pid_ -Force -ErrorAction Stop
        Write-Host "Stopped server (pid $pid_) on port $Port."
    } catch {
        Write-Host "Could not stop pid ${pid_}: $($_.Exception.Message)"
    }
}
