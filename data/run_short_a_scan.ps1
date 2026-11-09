$ErrorActionPreference = 'Continue'
$dataDir = $PSScriptRoot
$rootDir = Split-Path -Parent $dataDir
$dbPath = Join-Path $dataDir 'hiscores.sqlite3'
$logPath = Join-Path $dataDir 'a_full_scan.log'
$exportLogPath = Join-Path $dataDir 'a_full_export.log'
$statusPath = Join-Path $dataDir 'a_full_scan_status.json'
$csvPath = Join-Path $dataDir 'a_full_results.csv'
$jsonPath = Join-Path $dataDir 'a_full_results.json'
$startedAt = [DateTimeOffset]::UtcNow.ToString('o')

[ordered]@{
    status = 'running'
    pid = $PID
    startedAtUtc = $startedAt
    db = $dbPath
    log = $logPath
    csv = $csvPath
    json = $jsonPath
    names = 1333
    games = @('osrs', 'rs3')
    requestIntervalSeconds = 1
    timeoutSeconds = 10
    retries = 1
} | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $statusPath -Encoding UTF8

try {
    & python (Join-Path $rootDir 'hiscores_tool.py') --db $dbPath scan --game both --interval 1 --timeout 10 --retries 1 --retry-unknown *> $logPath
    $scanExit = $LASTEXITCODE
    & python (Join-Path $rootDir 'hiscores_tool.py') --db $dbPath export $csvPath *> $exportLogPath
    $csvExit = $LASTEXITCODE
    & python (Join-Path $rootDir 'hiscores_tool.py') --db $dbPath export $jsonPath *> $exportLogPath
    $jsonExit = $LASTEXITCODE
    $finalStatus = if ($scanExit -eq 0 -and $csvExit -eq 0 -and $jsonExit -eq 0) { 'completed' } else { 'stopped_or_failed' }
    [ordered]@{
        status = $finalStatus
        pid = $PID
        startedAtUtc = $startedAt
        finishedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
        scanExitCode = $scanExit
        csvExportExitCode = $csvExit
        jsonExportExitCode = $jsonExit
        db = $dbPath
        log = $logPath
        csv = $csvPath
        json = $jsonPath
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $statusPath -Encoding UTF8
} catch {
    [ordered]@{
        status = 'failed'
        pid = $PID
        startedAtUtc = $startedAt
        finishedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')
        error = $_.Exception.Message
        db = $dbPath
        log = $logPath
    } | ConvertTo-Json -Depth 3 | Set-Content -LiteralPath $statusPath -Encoding UTF8
}
