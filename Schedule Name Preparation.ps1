$ErrorActionPreference = 'Stop'
$taskName = 'Name Search - Daily Preparation'
$scriptPath = Join-Path $PSScriptRoot 'Prepare Name Search.ps1'
$powershell = (Get-Command powershell.exe -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $powershell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
# Windows uses the computer's configured local time zone. Set the PC to Eastern Time
# to run at 4:30 a.m. Eastern, an hour before the 5:30 a.m. preparation deadline.
$trigger = New-ScheduledTaskTrigger -Daily -At '4:30AM'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Starts the local Name Search server if needed and begins a scan using the saved letter batch.' -Force | Out-Null
Write-Output "Installed '$taskName' for 4:30 a.m. local computer time. Confirm Windows is set to Eastern Time for the intended schedule."
