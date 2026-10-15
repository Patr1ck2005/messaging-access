#Requires -Version 7.0
<#
.SYNOPSIS
Build an explicit public-source preview without Git initialization or upload.
.DESCRIPTION
Copies only maintained public source types into a new .local/release-* directory.
Local deployment state, downloaded programs and evidence are never selected.
#>
[CmdletBinding()]
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$required = @('README.md', 'AGENTS.md', '.gitignore', 'LICENSE', 'THIRD_PARTY.md',
    'requirements.lock', 'upstream-lock.json', 'config.example.json')
$selected = [Collections.Generic.List[object]]::new()
function Add-PublicFile([IO.FileInfo]$File) {
    if (($File.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Public source must not be a file link.' }
    $relative = [IO.Path]::GetRelativePath($root, $File.FullName).Replace('\', '/')
    if ($relative.StartsWith('../') -or $relative -match '(^|/)(vendor|\.venv|\.local|evidence|node_modules|__pycache__)(/|$)') {
        throw 'A private or external path entered the public file selection.'
    }
    $text = [IO.File]::ReadAllText($File.FullName)
    if ($text -match '(?i)[A-Z]:[/\\](Dev[/\\]|Users[/\\])') {
        throw "Machine-specific path found in public source: $relative"
    }
    if ($text -match '(?im)(?:accessToken|master_key|api_key)\s*[=:]\s*["''][A-Za-z0-9_\-]{24,}["'']') {
        throw "Possible literal credential found in public source: $relative"
    }
    $selected.Add([pscustomobject]@{ path = $relative; bytes = $File.Length;
        sha256 = (Get-FileHash -LiteralPath $File.FullName -Algorithm SHA256).Hash.ToLowerInvariant(); source = $File.FullName })
}
foreach ($name in $required) {
    $path = Join-Path $root $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing required public file: $name" }
    Add-PublicFile (Get-Item -LiteralPath $path)
}
$types = @{ docs = @('.md'); scripts = @('.ps1'); tests = @('.ps1');
    skills = @('.md', '.yaml', '.yml', '.py', '.ps1'); '.github' = @('.yaml', '.yml') }
foreach ($directory in $types.Keys) {
    $base = Join-Path $root $directory
    if (-not (Test-Path -LiteralPath $base)) { continue }
    if (((Get-Item -LiteralPath $base).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Linked public source directory $directory is not publishable."
    }
    $pending = [Collections.Generic.Stack[string]]::new(); $pending.Push($base)
    while ($pending.Count -gt 0) {
        foreach ($entry in Get-ChildItem -LiteralPath $pending.Pop() -Force) {
            if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "Linked source path under $directory is not publishable." }
            if ($entry.PSIsContainer) {
                if ($entry.Name -notin @('__pycache__', '.local', 'node_modules')) { $pending.Push($entry.FullName) }
            } elseif ($entry.Extension.ToLowerInvariant() -in $types[$directory]) { Add-PublicFile $entry }
        }
    }
}
$files = @($selected | Sort-Object path)
$manifestFiles = @($files | Select-Object path, bytes, sha256)
$bytes = [long]0; foreach ($file in $files) { $bytes += $file.bytes }
if (-not $CheckOnly) {
    $parent = Join-Path $root '.local'
    if ((Test-Path -LiteralPath $parent) -and ((Get-Item -LiteralPath $parent).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'The private preview parent must not be a link.'
    }
    [void][IO.Directory]::CreateDirectory($parent)
    $destination = Join-Path $parent ('release-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    [void][IO.Directory]::CreateDirectory($destination)
    foreach ($file in $files) {
        $target = Join-Path $destination $file.path
        [void][IO.Directory]::CreateDirectory((Split-Path $target -Parent))
        [IO.File]::Copy($file.source, $target, $false)
        if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ine $file.sha256) { throw 'Copied public source hash differs.' }
    }
    $manifest = [ordered]@{ schemaVersion = 1; files = $manifestFiles; sourceBytes = $bytes }
    [IO.File]::WriteAllText((Join-Path $destination 'release-manifest.json'), ($manifest | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    Write-Output ([pscustomobject]@{ status = 'public-preview-created'; directory = $destination; files = $files.Count; sourceBytes = $bytes; uploaded = $false })
} else {
    Write-Output ([pscustomobject]@{ status = 'public-source-check-passed'; files = $files.Count; sourceBytes = $bytes; uploaded = $false })
}
