# Name Search dashboard

The local dashboard is served by `name_server.py` at <http://127.0.0.1:8765/>. It talks only to that local server. The dashboard shows public Hiscores evidence separately from the runner's authenticated verdict; public lookup results do not confirm that a name is available.

## Start it

1. Confirm Python is installed and `name_server.py` is in this folder.
2. Double-click `Launch Name Search.cmd`. It opens a server window, then the dashboard at <http://127.0.0.1:8765/>. Keep the server window open while using the dashboard.
3. Select one or more starting letters under **Preparation batch** and save them. Start a scan from the page.

Check the selected day's batch against Jagex's [published release schedule](https://secure.runescape.com/m=news/name-release-starts-today) before saving it. The dashboard accepts a single letter or several letters together.

The dashboard refreshes automatically and does not reload the page. Unsaved letter edits stay selected while the page polls; save them to persist the batch. The selected batch is reused by scheduled preparation. Start and control the authenticated runner in Tampermonkey. To queue a shortlist candidate or typed one-off name, first start the runner there, then use the dashboard queue control. Public lookup remains a separate Hiscores-only check.

The scan orders names by length: one-character names, every two-character letter/number combination for each selected starting letter, then common dictionary words and curated people at lengths 3, 4, and so on through 12. Pinned names lead the list. Dictionary entries use the bundled [common-word filter](data/sources/common_words_provenance.md); obscure entries from the broad spelling lists are excluded from scans and the default shortlist. A specific excluded name can still be checked with the one-off controls or pinned.

Use **Export active names (.txt)** above the ranked shortlist to download every current shortlist name in rank order, one name per line. Switch to **Skipped** to export that view instead. The file contains names only, ready to paste or share.

## Schedule early preparation on Windows

After saving at least one batch letter, open PowerShell in this folder and run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File '.\Schedule Name Preparation.ps1'
```

The script replaces the same named task safely if run again. It creates a daily 4:30 a.m. task in the computer's local time zone; set Windows to Eastern Time for a 4:30 a.m. Eastern run, leaving an hour before 5:30 a.m. The task starts the local server in the background if it is not already running, then requests a scan for the saved batch. It does not change the batch or start the authenticated runner.

To run the preparation immediately, run `powershell.exe -NoProfile -ExecutionPolicy Bypass -File '.\Prepare Name Search.ps1'`. To remove the scheduled task, use Task Scheduler and delete **Name Search - Daily Preparation**.

The machine must be awake and signed in at the scheduled time. The task uses an interactive current-user principal so it does not require administrator credentials.
