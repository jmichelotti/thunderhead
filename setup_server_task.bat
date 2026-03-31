@echo off
:: Run this script as Administrator (right-click -> Run as administrator)
:: Registers the HLS Download Server to auto-start at logon.

echo Registering HLS Download Server scheduled task...
schtasks /create /tn "HLS Download Server" /tr "pythonw C:\dev\thunderhead-scripts\hls_download_server.py --apply" /sc onlogon /rl highest /f

if %errorlevel%==0 (
    echo.
    echo Success! The server will auto-start next time you log in.
    echo To start it now, run: restart_server.bat
) else (
    echo.
    echo Failed. Make sure you ran this as Administrator.
    echo Right-click setup_server_task.bat -^> Run as administrator
)
pause
