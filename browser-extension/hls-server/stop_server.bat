@echo off
powershell -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*hls_download_server*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Server stopped.
