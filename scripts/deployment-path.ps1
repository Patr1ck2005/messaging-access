# Shared path selection only. Callers parse the selected file and never retry another installation.
function Get-AccessProfileRoot {
    $profileRoot = $env:USERPROFILE
    if ([string]::IsNullOrWhiteSpace($profileRoot)) {
        $profileRoot = [Environment]::GetFolderPath('UserProfile')
    }
    if ([string]::IsNullOrWhiteSpace($profileRoot)) { throw 'The user profile directory is unavailable.' }
    [IO.Path]::GetFullPath($profileRoot)
}

function Get-AccessPrivateRoot {
    [IO.Path]::GetFullPath((Join-Path (Get-AccessProfileRoot) '.local/share/wechat-qq-access'))
}

function Resolve-AccessConfigPath {
    param([AllowNull()][AllowEmptyString()][string]$ConfigPath, [switch]$Explicit)
    if ($Explicit) {
        if ([string]::IsNullOrWhiteSpace($ConfigPath)) { throw 'An explicit configuration path cannot be empty.' }
        return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($ConfigPath))
    }
    if ($null -ne $env:MESSAGING_ACCESS_CONFIG) {
        if ([string]::IsNullOrWhiteSpace($env:MESSAGING_ACCESS_CONFIG)) { throw 'The deployment environment path is invalid.' }
        return [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($env:MESSAGING_ACCESS_CONFIG))
    }
    $fresh = Join-Path (Get-AccessPrivateRoot) 'deployment.json'
    if (Test-Path -LiteralPath $fresh -ErrorAction Stop) { return $fresh }
    $legacyRoot = $env:LOCALAPPDATA
    if ([string]::IsNullOrWhiteSpace($legacyRoot)) {
        $legacyRoot = Join-Path (Get-AccessProfileRoot) 'AppData/Local'
    }
    $legacy = Join-Path $legacyRoot 'MessagingAccess/deployment.json'
    if (Test-Path -LiteralPath $legacy -ErrorAction Stop) { return [IO.Path]::GetFullPath($legacy) }
    return $fresh
}
