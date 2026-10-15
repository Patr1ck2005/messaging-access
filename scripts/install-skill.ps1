#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ProfileRoot = [Environment]::GetFolderPath('UserProfile')
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This deployment requires Windows and PowerShell 7.' }
$source = [IO.Path]::GetFullPath((Join-Path (Split-Path $PSScriptRoot -Parent) 'skills/messaging-access'))
if (-not (Test-Path -LiteralPath (Join-Path $source 'SKILL.md') -PathType Leaf)) { throw 'Shared skill source is missing.' }
$profilePath = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ProfileRoot))
foreach ($relative in @('.codex/skills', '.agents/skills', '.dsh/skills')) {
    $parent = Join-Path $profilePath $relative
    $destination = Join-Path $parent 'messaging-access'
    $existing = Get-Item -LiteralPath $destination -Force -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        if ($existing.LinkType -notin @('Junction', 'SymbolicLink')) { throw "Existing skill path is not a managed link: $destination" }
        $target = [string](@($existing.Target)[0])
        if (-not [IO.Path]::IsPathRooted($target)) { $target = Join-Path $parent $target }
        if ([IO.Path]::GetFullPath($target).TrimEnd('\', '/') -ine $source.TrimEnd('\', '/')) {
            throw "Existing skill link points elsewhere; no overwrite: $destination"
        }
        Write-Output "Skill link already correct: $relative/messaging-access"
        continue
    }
    if ($PSCmdlet.ShouldProcess($destination, 'Create junction to the single shared skill source')) {
        $null = New-Item -ItemType Directory -Path $parent -Force
        $null = New-Item -ItemType Junction -Path $destination -Target $source
        Write-Output "Skill link installed: $relative/messaging-access"
    }
}
