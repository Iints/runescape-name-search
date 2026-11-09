@echo off
setlocal
cd /d "%~dp0"
start "Name Search Server" python name_server.py
timeout /t 2 /nobreak >nul
start "Name Search Dashboard" "http://127.0.0.1:8765/"
endlocal
