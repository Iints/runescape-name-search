param([switch]$NoStartServer)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$base = 'http://127.0.0.1:8765'

function Test-NameServer {
    try {
        $null = Invoke-RestMethod -Uri "$base/api/state" -TimeoutSec 3
        return $true
    } catch { return $false }
}

Set-Location -LiteralPath $root
if (-not (Test-NameServer)) {
    if ($NoStartServer) { throw 'Name Search server is not running at 127.0.0.1:8765.' }
    $python = Get-Command python -ErrorAction Stop
    Start-Process -FilePath $python.Source -ArgumentList @('name_server.py') -WorkingDirectory $root -WindowStyle Hidden
    $ready = $false
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        if (Test-NameServer) { $ready = $true; break }
    }
    if (-not $ready) { throw 'Name Search server did not become ready within 30 seconds.' }
}

# The server owns the persisted batch selection. Starting a scan here reuses it.
$null = Invoke-RestMethod -Method Post -Uri "$base/api/scan/start" -ContentType 'application/json' -Body '{}' -TimeoutSec 15
Write-Output "Preparation scan requested at $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')."
