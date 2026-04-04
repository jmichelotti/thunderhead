@echo off
:: Kills any running hls_download_server.py and restarts it.
:: Usage: double-click or run from terminal.

echo Stopping HLS Download Server...
taskkill /F /FI "IMAGENAME eq pythonw.exe" /FI "WINDOWTITLE eq *hls_download*" >nul 2>&1
taskkill /F /FI "IMAGENAME eq python.exe" /FI "WINDOWTITLE eq *hls_download*" >nul 2>&1

:: Also kill by command line match (catches pythonw which has no window title)
for /f "tokens=2" %%p in ('wmic process where "CommandLine like '%%hls_download_server%%'" get ProcessId /value 2^>nul ^| findstr ProcessId') do (
    taskkill /F /PID %%p >nul 2>&1
)

:: Fallback: kill anything holding port 9876 (in case wmic missed it)
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":9876.*LISTENING" 2^>nul') do (
    taskkill /F /PID %%p >nul 2>&1
)

timeout /t 1 /nobreak >nul

echo Starting HLS Download Server (background, --apply)...
start "" pythonw "C:\dev\thunderhead\browser-extension\hls-server\hls_download_server.py" --apply

echo Done. Server is running in background.
echo Check log: python read_server_log.py
pause
