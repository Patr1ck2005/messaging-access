#Requires -Version 7.0
# Dependency-free integration checks. Only the freshly created .local fixture
# is modified; live deployment is never cleaned by this test.
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$project = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$script = Join-Path $project 'scripts/cache.ps1'
$fixture = Join-Path $project ('.local/cache-tests-' + [Guid]::NewGuid().ToString('N'))
if (-not $fixture.StartsWith((Join-Path $project '.local') + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Fixture escaped the checkout.'
}
$private = Join-Path $fixture 'MessagingAccess'
$work = Join-Path $private 'wechat'
$outside = Join-Path $fixture 'original-client-data'
$configPath = Join-Path $fixture 'deployment.json'
$passed = 0
$linkMade = $false

function Assert([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:passed++
}
function New-OldFile([string]$Path, [int]$Bytes = 13) {
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($Path))
    [IO.File]::WriteAllBytes($Path, [byte[]]::new($Bytes))
    [IO.File]::SetLastWriteTimeUtc($Path, [DateTime]::UtcNow.AddDays(-10))
}
function Write-Config([string]$PrivatePath, [string]$PythonPath = (Join-Path $fixture 'not-running/python.exe')) {
    [ordered]@{
        schemaVersion = 1; projectRoot = $project; privateRoot = $PrivatePath
        pythonExe = $PythonPath
        qqRoot = Join-Path $project 'vendor/qq/v6.3.2'
        qqSecurityFile = Join-Path $project 'vendor/qq/runtime/config/security.json'
        qqExe = Join-Path $fixture 'QQ.exe'; qqPort = 40653
    } | ConvertTo-Json | Set-Content -LiteralPath $configPath -Encoding utf8
}

try {
    [void][IO.Directory]::CreateDirectory($work)
    Write-Config $private
    New-OldFile (Join-Path $work 'message__message_0.db') 97
    New-OldFile (Join-Path $work 'message__message_0.db.stamp') 5
    New-OldFile (Join-Path $work 'message__message_0.db.123.abc.tmp.wal-shm') 7
    New-OldFile (Join-Path $work 'keys.json')
    New-OldFile (Join-Path $work 'keys.json.bak')
    New-OldFile (Join-Path $work 'image_keys.json')
    New-OldFile (Join-Path $work 'unknown.sqlite')
    New-OldFile (Join-Path $work 'important.txt')
    New-OldFile (Join-Path $outside 'message__message_0.db') 101
    New-OldFile (Join-Path $private 'wechat-keys/stable.json')
    New-OldFile (Join-Path $private 'tmp/wechatauto_db/account-fixture/contact__contact.db') 11
    New-OldFile (Join-Path $private 'media/image.jpg') 17
    New-OldFile (Join-Path $private 'wechat/snapshots/history-20261001T010203Z.sqlite') 19
    New-OldFile (Join-Path $private 'wechat/snapshots/history-20261001T010203Z.json') 23
    New-OldFile (Join-Path $private 'wechat/snapshots/history.sqlite') 41
    New-OldFile (Join-Path $private 'wechat/snapshots/history.sqlite-wal') 43
    New-OldFile (Join-Path $private 'wechat/snapshots/history.json') 47
    New-OldFile (Join-Path $private 'wechat/snapshots/my-kept-archive.sqlite') 29
    New-OldFile (Join-Path $work 'session__session.db') 31
    [IO.File]::SetLastWriteTimeUtc((Join-Path $work 'session__session.db'), [DateTime]::UtcNow)

    # A junction is supported without Developer Mode on Windows. The target is
    # outside the allowed media directory and must never be followed.
    $link = Join-Path $private 'media/escape'
    New-Item -ItemType Junction -Path $link -Target $outside | Out-Null
    $linkMade = $true
    $status = & $script -ConfigPath $configPath
    Assert ($status.Action -eq 'Status') 'Default action must only inspect.'
    Assert ($status.Categories.Count -eq 4) 'Status must show every category.'
    Assert ($status.RemovedBytes -eq 0) 'Status must not claim removals.'
    Assert ($status.SkippedReparsePoints -ge 1) 'Linked media paths must be skipped.'
    $minimumAge = & $script -ConfigPath $configPath -OlderThanDays 0
    Assert ($minimumAge.MinimumAgeDays -eq 0) 'Explicit zero-age preview must be supported.'
    Assert ((Get-Item -LiteralPath (Join-Path $outside 'message__message_0.db')).Length -eq 101) 'Original data changed.'
    Assert (-not (Test-Path -LiteralPath (Join-Path $private 'maintenance'))) 'Status created a journal.'

    $preview = & $script -ConfigPath $configPath -Action Clean -Category WeChatCache -WhatIf
    Assert ($preview.RemovedBytes -eq 0) 'WhatIf must not claim deletions.'
    Assert (Test-Path -LiteralPath (Join-Path $work 'message__message_0.db')) 'WhatIf deleted a cache.'
    Assert (-not (Test-Path -LiteralPath (Join-Path $private 'maintenance'))) 'WhatIf wrote a journal.'

    # A Python task elsewhere in this checkout must not block an independent
    # fixture. The configured interpreter itself must still block cleanup.
    $testPython = Join-Path $project '.venv/Scripts/python.exe'
    if (Test-Path -LiteralPath $testPython -PathType Leaf) {
        $readerScript = Join-Path $fixture 'reader.py'
        'import time; time.sleep(60)' | Set-Content -LiteralPath $readerScript -Encoding utf8
        $reader = Start-Process -FilePath $testPython -ArgumentList ('"' + $readerScript + '"') -WindowStyle Hidden -PassThru
        try {
            $unrelated = & $script -ConfigPath $configPath
            Assert (-not $unrelated.WeChatReaderActive) 'An unrelated Python task in projectRoot blocked fixture cleanup.'
            Write-Config $private $testPython
            $active = & $script -ConfigPath $configPath -OlderThanDays 0
            Assert $active.WeChatReaderActive 'Configured Python readers must be detected.'
            Assert ($active.Categories[0].ReclaimableFiles -eq 0) 'Active configured readers must block cleanup even at zero age.'
        } finally {
            if (-not $reader.HasExited) { Stop-Process -Id $reader.Id -Force }
            $reader.WaitForExit()
            Write-Config $private
        }
        $reader = Start-Process -FilePath $testPython -ArgumentList ('"' + $readerScript + '" "' + $private + '"') -WindowStyle Hidden -PassThru
        try {
            $active = & $script -ConfigPath $configPath
            Assert $active.WeChatReaderActive 'A generic Python task naming this privateRoot must be detected.'
        } finally {
            if (-not $reader.HasExited) { Stop-Process -Id $reader.Id -Force }
            $reader.WaitForExit()
        }
    }

    # Hold one eligible file without sharing; cleanup must skip it.
    $lockedPath = Join-Path $work 'contact__contact.db'
    New-OldFile $lockedPath 37
    $lock = [IO.File]::Open($lockedPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
    try { $clean = & $script -ConfigPath $configPath -Action Clean -Category WeChatCache -Confirm:$false }
    finally { $lock.Dispose() }
    Assert ($clean.RemovedBytes -eq 120) 'Actual removal count must equal the four unlinked fixture files.'
    Assert (Test-Path -LiteralPath $lockedPath) 'An in-use cache was deleted.'
    Assert ($clean.Categories[0].FailedOrChangedFiles -eq 1) 'Locked-file skip was not recorded.'
    foreach ($name in @('keys.json', 'keys.json.bak', 'image_keys.json', 'unknown.sqlite', 'important.txt', 'session__session.db')) {
        Assert (Test-Path -LiteralPath (Join-Path $work $name)) 'A preserved or recent file was deleted.'
    }
    Assert (Test-Path -LiteralPath (Join-Path $private 'wechat-keys/stable.json')) 'Stable keys were removed.'
    $journal = @(Get-ChildItem -LiteralPath (Join-Path $private 'maintenance') -File)
    Assert ($journal.Count -eq 1) 'Successful cleanup must create a private journal.'
    $records = @(Get-Content -LiteralPath $journal[0].FullName | ForEach-Object { $_ | ConvertFrom-Json })
    Assert ([long](($records | Measure-Object removedBytes -Sum).Sum) -eq 120) 'Journal disagrees with actual removed bytes.'

    # All includes deployment logs; preview it only so this test cannot touch
    # any live QQ file, even when that server is stopped.
    $all = & $script -ConfigPath $configPath -Action Clean -Category All -WhatIf
    Assert ($all.Categories.Count -eq 2) 'Generic cleanup must target only caches and logs.'
    Assert (Test-Path -LiteralPath (Join-Path $private 'media/image.jpg')) 'All deleted media without explicit opt-in.'
    Assert (Test-Path -LiteralPath (Join-Path $private 'wechat/snapshots/history-20261001T010203Z.sqlite')) 'All deleted a snapshot without explicit opt-in.'
    $snapshots = & $script -ConfigPath $configPath -Action Clean -Category Snapshots -Confirm:$false
    Assert ($snapshots.RemovedBytes -eq 173) 'Explicit snapshot cleanup did not remove recognized fixed/timestamped fixture exports.'
    Assert (Test-Path -LiteralPath (Join-Path $private 'wechat/snapshots/my-kept-archive.sqlite')) 'Unknown archive names must remain.'
    $media = & $script -ConfigPath $configPath -Action Clean -Category WeChatMedia -Confirm:$false
    Assert ($media.RemovedBytes -eq 17) 'Explicit media cleanup failed or followed a link.'
    Assert ((Get-Item -LiteralPath (Join-Path $outside 'message__message_0.db')).Length -eq 101) 'Link target was changed.'
    $fresh = & $script -ConfigPath $configPath -Action Clean -Category WeChatCache -OlderThanDays 0 -Confirm:$false
    Assert ($fresh.RemovedBytes -eq 68) 'Explicit zero age must clear a fresh recognized cache and the now-unlocked old cache.'
    Assert (-not (Test-Path -LiteralPath (Join-Path $work 'session__session.db'))) 'Zero-age cleanup retained a fresh cache.'
    Assert (Test-Path -LiteralPath (Join-Path $work 'keys.json')) 'Zero-age cleanup removed keys.'

    # Even with a valid projectRoot, dangerous private roots must be rejected.
    foreach ($unsafe in @([IO.Path]::GetPathRoot($project), $HOME, $project, (Join-Path $fixture 'xwechat_files/MessagingAccess'))) {
        Write-Config $unsafe
        $rejected = $false
        try { & $script -ConfigPath $configPath | Out-Null } catch { $rejected = $true }
        Assert $rejected 'An unsafe privateRoot was accepted.'
    }
    Write-Config $private
    [pscustomobject]@{ Passed = $passed; LiveCleanup = $false; FixtureOnly = $true }
} finally {
    # Remove the junction itself first. Never recurse through its target.
    if ($linkMade -and (Test-Path -LiteralPath $link)) { [IO.Directory]::Delete($link) }
    $resolvedFixture = [IO.Path]::GetFullPath($fixture)
    $allowedBase = [IO.Path]::GetFullPath((Join-Path $project '.local')) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedFixture.StartsWith($allowedBase, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($resolvedFixture) -notmatch '^cache-tests-[0-9a-f]{32}$') {
        throw 'Refusing unsafe test cleanup.'
    }
    if (Test-Path -LiteralPath $resolvedFixture) {
        $linked = @(Get-ChildItem -LiteralPath $resolvedFixture -Recurse -Force -Attributes ReparsePoint)
        if ($linked.Count -gt 0) { throw 'Unexpected link in test cleanup; fixture retained.' }
        Remove-Item -LiteralPath $resolvedFixture -Recurse -Force
    }
}
