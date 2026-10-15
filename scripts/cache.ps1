#Requires -Version 7.0
<#
.SYNOPSIS
Preview storage or remove old, recognized copies made by messaging-access.
.EXAMPLE
./scripts/cache.ps1 -Json
.EXAMPLE
./scripts/cache.ps1 -Action Clean -Category WeChatCache,Logs -OlderThanDays 7 -WhatIf
.NOTES
No original chat stores, credentials, exports outside the dedicated snapshot
folder, programs, unknown files, or linked directories are cleanup targets.
Close active message-reading commands before cleaning. QQ logs are skipped
while the configured server is listening. No automatic cleanup is installed.
#>
[CmdletBinding(SupportsShouldProcess, ConfirmImpact = 'Medium')]
param(
    [ValidateSet('Status', 'Clean')][string]$Action = 'Status',
    [ValidateSet('WeChatCache', 'WeChatMedia', 'Snapshots', 'Logs', 'All')]
    [string[]]$Category = @('All'),
    [ValidateRange(0, 36500)][double]$OlderThanDays = 7,
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'MessagingAccess/deployment.json'),
    [switch]$Json
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$comparison = [StringComparison]::OrdinalIgnoreCase
$reparse = [IO.FileAttributes]::ReparsePoint

function Get-AbsolutePath([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathFullyQualified($Path) -or
        $Path.StartsWith('\\')) { throw 'An absolute local path is required.' }
    return [IO.Path]::GetFullPath($Path).TrimEnd([IO.Path]::DirectorySeparatorChar)
}

function Test-InDirectory([string]$Path, [string]$Directory) {
    return $Path.StartsWith($Directory + [IO.Path]::DirectorySeparatorChar, $comparison)
}

function Assert-NoLinks([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while (-not [string]::IsNullOrEmpty($cursor)) {
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band $reparse) -ne 0) {
                throw 'A configured path contains a reparse point; refusing traversal.'
            }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
}

# deployment.json contains paths only. Never read the referenced security file.
$configFull = Get-AbsolutePath $ConfigPath
Assert-NoLinks $configFull
$config = Get-Content -LiteralPath $configFull -Raw | ConvertFrom-Json
if ($config.schemaVersion -ne 1) { throw 'Unsupported deployment schemaVersion.' }
$project = Get-AbsolutePath $config.projectRoot
$expectedProject = Get-AbsolutePath (Join-Path $PSScriptRoot '..')
if (-not $project.Equals($expectedProject, $comparison)) { throw 'projectRoot must match this checkout.' }
$private = Get-AbsolutePath $config.privateRoot
$qq = Get-AbsolutePath $config.qqRoot
$python = Get-AbsolutePath $config.pythonExe
if (-not ([IO.Path]::GetFileName($private)).Equals('MessagingAccess', $comparison) -or
    $private.Equals($project, $comparison) -or (Test-InDirectory $project $private) -or
    $private.Equals((Get-AbsolutePath $HOME), $comparison) -or
    $private.Equals((Get-AbsolutePath $env:LOCALAPPDATA), $comparison)) {
    throw 'privateRoot must be a dedicated MessagingAccess directory.'
}
if ($private -match '(?i)[\\/](xwechat_files|WeChat Files|Tencent|QQChatExporter|\.qq-chat-exporter|\.codex|\.dsh|Windows|Program Files(?: \(x86\))?|vendor|\.venv|node_modules)(?:[\\/]|$)') {
    throw 'privateRoot cannot be inside original chat, credential, system or program directories.'
}
if (-not $qq.Equals((Get-AbsolutePath (Join-Path $project 'vendor/qq/v6.3.2')), $comparison)) {
    throw "qqRoot must be this deployment's vendor/qq/v6.3.2 directory."
}
foreach ($root in @($project, $private, $qq)) { Assert-NoLinks $root }
if ($config.qqPort -isnot [ValueType] -or $config.qqPort -lt 1 -or $config.qqPort -gt 65535) {
    throw 'qqPort must be a valid TCP port.'
}

$readerActive = $false
$qqActive = $false
$processCheckAvailable = $true
try {
    $readerActive = @(
        Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object {
            ($_.ExecutablePath -and $_.ExecutablePath.Equals($python, $comparison)) -or
            ($_.CommandLine -and $_.CommandLine.Contains($private, $comparison))
        }
    ).Count -gt 0
    $qqActive = @(Get-NetTCPConnection -LocalPort ([int]$config.qqPort) -State Listen -ErrorAction SilentlyContinue).Count -gt 0
} catch {
    # Missing process visibility must not turn into permission to remove caches.
    $processCheckAvailable = $false
    $readerActive = $true
    $qqActive = $true
}

$categories = @('WeChatCache', 'WeChatMedia', 'Snapshots', 'Logs')
$selected = if ($Category -contains 'All') {
    # Status shows every category. Generic cleanup leaves intentional downloads
    # and full-history snapshots alone unless named explicitly.
    $defaults = if ($Action -eq 'Status') { $categories } else { @('WeChatCache', 'Logs') }
    @($defaults + @($Category | Where-Object { $_ -ne 'All' }) | Select-Object -Unique)
} else { @($Category | Select-Object -Unique) }
$cutoff = [DateTime]::UtcNow.AddDays(-$OlderThanDays)
$items = [Collections.Generic.List[object]]::new()
$skippedLinks = 0

function Find-RegularFiles([string]$Directory, [bool]$Recurse = $true) {
    Assert-NoLinks $Directory
    if (-not (Test-Path -LiteralPath $Directory -PathType Container)) { return }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Directory)
    while ($pending.Count -gt 0) {
        foreach ($entry in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if (($entry.Attributes -band $reparse) -ne 0) { $script:skippedLinks++; continue }
            if ($entry.PSIsContainer) {
                if ($Recurse) { $pending.Push($entry.FullName) }
            } else { $entry }
        }
    }
}

function Add-Files([string]$Kind, [string]$Directory, [string]$Pattern, [bool]$Recurse, [bool]$Blocked) {
    foreach ($file in Find-RegularFiles $Directory $Recurse) {
        # JSON, key backups and unrecognized names are always retained.
        if ($file.Name -notmatch $Pattern -or $file.Name -match '(?i)(keys?|credentials?|security)\.|\.bak$' -or
            ($Kind -ne 'Snapshots' -and $file.Name -match '(?i)\.json(?:\.|$)')) { continue }
        $items.Add([pscustomobject]@{
            Category = $Kind; Path = $file.FullName; Length = [long]$file.Length
            Modified = $file.LastWriteTimeUtc; Eligible = ($file.LastWriteTimeUtc -lt $cutoff -and -not $Blocked)
        })
    }
}

# Flattened names are produced by rel.replace(os.sep, '__') in WeChatDB.
# Sidecars and process/thread temporary variants belong to that same cache.
$dbPattern = '(?i)^[a-z0-9_]+\.db(?:\.stamp|-wal|-shm|\.[0-9]+\.[0-9a-f]+\.tmp(?:\.wal)?(?:-wal|-shm)?)?$'
$wechat = Join-Path $private 'wechat'
Add-Files 'WeChatCache' $wechat $dbPattern $false $readerActive
$fallback = Join-Path $private 'tmp/wechatauto_db'
Add-Files 'WeChatCache' $fallback $dbPattern $true $readerActive
$mediaPattern = '(?i)\.(jpg|jpeg|png|gif|webp|bmp|heic|tif|tiff|mp4|mov|mkv|avi|webm|mp3|wav|silk|amr|ogg|m4a|aac|flac|dat)$'
foreach ($dir in @((Join-Path $private 'media'), (Join-Path $wechat 'media'))) {
    Add-Files 'WeChatMedia' $dir $mediaPattern $true $readerActive
}
# Whole-history exports remain opt-in and have their own category.
Add-Files 'Snapshots' (Join-Path $wechat 'snapshots') '(?i)^history(?:-[0-9]{8}T[0-9]{6}[0-9]*Z)?\.(?:(?:sqlite|db)(?:-wal|-shm)?|json)$' $false $readerActive
$logPattern = '(?i)\.log(?:\.[0-9-]+)?$'
foreach ($dir in @((Join-Path $qq 'logs'), (Join-Path (Split-Path $qq -Parent) 'runtime/logs'))) {
    Add-Files 'Logs' $dir $logPattern $true $qqActive
}

$removed = @{}
$failed = @{}
foreach ($kind in $categories) { $removed[$kind] = [long]0; $failed[$kind] = 0 }
$removedCount = @{}
foreach ($kind in $categories) { $removedCount[$kind] = 0 }
$journalWriter = $null
try {
    if ($Action -eq 'Clean') {
        $approved = @{}
        foreach ($kind in $selected) {
            $candidates = @($items | Where-Object { $_.Category -eq $kind -and $_.Eligible })
            $candidateBytes = [long]0
            foreach ($candidate in $candidates) { $candidateBytes += $candidate.Length }
            # Aggregate prompts avoid revealing filenames and keep WhatIf short.
            $approved[$kind] = $candidates.Count -gt 0 -and $PSCmdlet.ShouldProcess(
                "$kind ($($candidates.Count) files, $([Math]::Round($candidateBytes / 1MB, 3)) MiB)",
                'Remove recognized cached copies')
        }
        foreach ($item in $items) {
            if ($item.Category -notin $selected -or -not $item.Eligible) { continue }
            if (-not $approved[$item.Category]) { continue }
            $handle = $null
            try {
                Assert-NoLinks $item.Path
                $current = Get-Item -LiteralPath $item.Path -Force
                if ($current.PSIsContainer -or $current.Length -ne $item.Length -or
                    $current.LastWriteTimeUtc -ne $item.Modified -or $current.LastWriteTimeUtc -ge $cutoff) {
                    $failed[$item.Category]++; continue
                }
                # Share Delete permits our unlink, while denying concurrent
                # readers/writers. Open fails if a consumer holds the file.
                $handle = [IO.File]::Open($item.Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Delete)
                if ($null -eq $journalWriter) {
                    $journalDir = Join-Path $private 'maintenance'
                    Assert-NoLinks $journalDir
                    [void][IO.Directory]::CreateDirectory($journalDir)
                    $journalPath = Join-Path $journalDir ('cleanup-' + [DateTime]::UtcNow.ToString('yyyy-MM') + '.jsonl')
                    Assert-NoLinks $journalPath
                    $journalWriter = [IO.StreamWriter]::new($journalPath, $true, [Text.UTF8Encoding]::new($false))
                }
                Assert-NoLinks $item.Path
                [IO.File]::Delete($item.Path)
                $handle.Dispose(); $handle = $null
                if (-not (Test-Path -LiteralPath $item.Path)) {
                    $removed[$item.Category] += $item.Length
                    $removedCount[$item.Category]++
                    $record = [ordered]@{
                        utc = [DateTime]::UtcNow.ToString('o'); category = $item.Category
                        path = $item.Path; removedBytes = $item.Length
                    }
                    $journalWriter.WriteLine(($record | ConvertTo-Json -Compress))
                    $journalWriter.Flush()
                } else { $failed[$item.Category]++ }
            } catch {
                # No raw exception text: it could include private filenames.
                $failed[$item.Category]++
            } finally { if ($null -ne $handle) { $handle.Dispose() } }
        }
    }
} finally { if ($null -ne $journalWriter) { $journalWriter.Dispose() } }

$summary = foreach ($kind in $selected) {
    $files = @($items | Where-Object Category -eq $kind)
    $eligible = @($files | Where-Object Eligible)
    $size = [long]0
    $reclaimable = [long]0
    foreach ($file in $files) { $size += $file.Length }
    foreach ($file in $eligible) { $reclaimable += $file.Length }
    [pscustomobject]@{
        Category = $kind; Files = $files.Count; MiB = [Math]::Round($size / 1MB, 3)
        ReclaimableFiles = $eligible.Count; ReclaimableMiB = [Math]::Round($reclaimable / 1MB, 3)
        RetainedFiles = $files.Count - $eligible.Count; RemovedFiles = $removedCount[$kind]
        RemovedBytes = $removed[$kind]; FailedOrChangedFiles = $failed[$kind]
    }
}
$totalRemoved = [long]0
foreach ($row in $summary) { $totalRemoved += $row.RemovedBytes }
$result = [pscustomobject]@{
    Action = $Action; MinimumAgeDays = $OlderThanDays
    WeChatReaderActive = $readerActive; QQServerActive = $qqActive
    ProcessCheckAvailable = $processCheckAvailable; SkippedReparsePoints = $skippedLinks
    Categories = @($summary); RemovedBytes = $totalRemoved
    Note = 'All cleanup means WeChatCache and Logs; name WeChatMedia or Snapshots explicitly to remove those copies. Only recognized private copies are listed. Original stores, keys, unknown files and QQ media/exports are preserved. Reclaimable is an age/process preview; locked or changed files may be skipped.'
}
if ($Json) { $result | ConvertTo-Json -Depth 5 } else { $result }
