$ErrorActionPreference = 'Stop'
[void][Reflection.Assembly]::LoadWithPartialName('System.Net.Http')
$names = @('A', 'Al', 'Ace', 'Ada', 'Air', 'Ash')
$base = 'https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player='
$client = [System.Net.Http.HttpClient]::new()
$client.Timeout = [TimeSpan]::FromSeconds(10)
$client.DefaultRequestHeaders.UserAgent.ParseAdd('RareNameResearchProbe/0.1')
$results = [System.Collections.Generic.List[object]]::new()
$stopProvider = $false
$lastStart = $null
$observedAtUtc = [DateTimeOffset]::UtcNow.ToString('o')

try {
    foreach ($name in $names) {
        if ($null -ne $lastStart) {
            $remaining = 1000 - [int]([DateTimeOffset]::UtcNow - $lastStart).TotalMilliseconds
            if ($remaining -gt 0) { Start-Sleep -Milliseconds $remaining }
        }
        $lastStart = [DateTimeOffset]::UtcNow
        $uri = $base + [Uri]::EscapeDataString($name)
        $status = $null
        $contentType = $null
        $valid = $false
        $totalLevel = $null
        $errorText = $null

        try {
            $response = $client.GetAsync($uri).GetAwaiter().GetResult()
            $status = [int]$response.StatusCode
            if ($null -ne $response.Content.Headers.ContentType) {
                $contentType = $response.Content.Headers.ContentType.ToString()
            }
            if ($status -eq 403 -or $status -eq 429) {
                $stopProvider = $true
            } elseif ($response.IsSuccessStatusCode -and $contentType -match '(^|/)json') {
                $body = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
                try {
                    $data = $body | ConvertFrom-Json
                    $skillsAreArray = $data.skills -is [array]
                    $activitiesAreArray = $data.activities -is [array]
                    $skillRows = @($data.skills)
                    $activityRows = @($data.activities)
                    $overall = $skillRows | Where-Object { $_.name -eq 'Overall' -or $_.id -eq 0 } | Select-Object -First 1
                    $skillFieldsValid = $skillRows.Count -gt 0 -and $null -ne $skillRows[0].rank -and $null -ne $skillRows[0].level -and $null -ne $skillRows[0].xp
                    $activityFieldsValid = $activityRows.Count -eq 0 -or ($null -ne $activityRows[0].rank -and $null -ne $activityRows[0].score)
                    $overallValid = $null -ne $overall -and $overall.name -eq 'Overall' -and $null -ne $overall.rank -and $null -ne $overall.level -and $null -ne $overall.xp
                    $valid = $skillsAreArray -and $activitiesAreArray -and $skillFieldsValid -and $activityFieldsValid -and $overallValid
                    if ($valid) { $totalLevel = $overall.level }
                } catch {
                    $errorText = 'JSON parse failed'
                }
            }
            $response.Dispose()
        } catch {
            $ex = $_.Exception
            if ($null -ne $ex.Response) {
                try { $status = [int]$ex.Response.StatusCode } catch {}
                if ($status -eq 403 -or $status -eq 429) { $stopProvider = $true }
                try { $contentType = $ex.Response.Content.Headers.ContentType.ToString() } catch {}
            } else {
                $errorText = $ex.GetBaseException().Message
            }
        }

        $results.Add([pscustomobject]@{
            name = $name
            status_code = $status
            content_type = $contentType
            skills_activities_structure_valid = $valid
            total_level = $totalLevel
            error = $errorText
        })
        if ($stopProvider) { break }
    }
} finally {
    $client.Dispose()
}

$results | ConvertTo-Json -Depth 4
$output = [pscustomobject]@{ observedAtUtc = $observedAtUtc; endpoint = 'https://secure.runescape.com/m=hiscore_oldschool/index_lite.json?player={name}'; userAgent = 'RareNameResearchProbe/0.1'; results = @($results) }
$output | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'hiscores_probe_results.json') -Encoding UTF8
if ($stopProvider) { Write-Output 'STOPPED: provider returned 403/429; no more requests sent.' }
