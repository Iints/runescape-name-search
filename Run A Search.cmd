@echo off
cd /d "%~dp0"
title RuneScape A Name Search

if not exist data mkdir data
if not exist data\a_candidates.json (
    echo Missing data\a_candidates.json. The candidate corpus must be placed in the data folder first.
    pause
    exit /b 1
)

echo Updating the local A-name corpus...
python hiscores_tool.py --db data\hiscores.sqlite3 import data\a_candidates.json --source a_candidates.json
if errorlevel 1 (
    echo Corpus import failed. See the message above.
    pause
    exit /b 1
)

set REFRESHFLAG=
set /p REFRESH=Refresh previously checked names too? Enter Y to refresh, or press Enter to resume: 
if /I "%REFRESH%"=="Y" set REFRESHFLAG=--refresh

echo.
echo Search uses a configurable client pace of up to 3 requests/second, with at most 4 workers.
echo This is not a claim about Jagex's published rate limit. A 403 or 429 stops the scan.
echo The output shortlist contains only names with 404 responses from both games.
echo.
python hiscores_fast.py --db data\hiscores.sqlite3 --letter A --min-length 3 --rate 3 --workers 4 --duration 600 --timeout 10 --retries 1 --retry-unknown %REFRESHFLAG%
echo.
echo Shortlist files, when any names qualify: data\a_shortlist.csv and data\a_shortlist.json
pause
