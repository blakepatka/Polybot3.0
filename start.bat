@echo off
REM ==========================================================================
REM  Start the Polybot dashboard on localhost and open it in your browser.
REM
REM    start.bat          -- serve on the default port 8848
REM    start.bat 9000     -- serve on port 9000
REM
REM  Double-clicking this file works from anywhere; it switches to its own
REM  folder first. This is a thin wrapper over start.ps1, which supervises
REM  run.py and relaunches it if it dies, so all the restart and logging
REM  behaviour lives in one place.
REM
REM  Close this window or press Ctrl+C to stop. To stop it from elsewhere,
REM  run stop.ps1.
REM ==========================================================================

setlocal
cd /d "%~dp0"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8848"

where py >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Could not find the "py" launcher on PATH.
  echo   Install Python 3.13 or newer from python.org, then run this again.
  echo.
  pause
  exit /b 1
)

REM Never launch a second engine against a port that is already served.
REM start.ps1 checks this once before it starts supervising, but its restart
REM loop would otherwise keep relaunching run.py into a port it can never
REM bind -- ten fast failures and it gives up. Catch it here instead and just
REM show the dashboard that is already running.
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "if (Get-NetTCPConnection -State Listen -LocalPort %PORT% -ErrorAction SilentlyContinue) { exit 1 }"
if errorlevel 1 (
  echo.
  echo   Polybot is already serving port %PORT% - opening the dashboard.
  echo   Run stop.ps1 first if you meant to restart it.
  echo.
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)

echo.
echo   Polybot  -^>  http://127.0.0.1:%PORT%
echo   Close this window or press Ctrl+C to stop.
echo.

REM Open the dashboard once the server has had a moment to bind. Runs in its
REM own minimised window so it cannot hold up the server starting.
start "" /min powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:%PORT%'"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Port %PORT%

set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
  echo.
  echo   Polybot exited with code %CODE%. See logs\polybot.log for details.
  echo.
  pause
)

endlocal
exit /b %CODE%
