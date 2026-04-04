@echo off
start "HLS Download Server" cmd /k "cd /d C:\dev\thunderhead\browser-extension\hls-server && python hls_download_server.py --apply"
