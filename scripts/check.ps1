#Requires -Version 7.0
[CmdletBinding()]
param([string]$ConfigPath)
$ErrorActionPreference = 'Stop'
$pathParameters = @{} + $PSBoundParameters
. (Join-Path $PSScriptRoot 'deployment-path.ps1')
$ConfigPath = Resolve-AccessConfigPath -ConfigPath $ConfigPath -Explicit:$($pathParameters.ContainsKey('ConfigPath'))
if (-not $IsWindows) { throw 'This deployment requires Windows and PowerShell 7.' }
try { $config = Get-Content -LiteralPath ([Environment]::ExpandEnvironmentVariables($ConfigPath)) -Raw | ConvertFrom-Json }
catch { throw 'Private deployment config is unavailable or invalid. No credential values are displayed.' }
if ($config.schemaVersion -ne 1) { throw 'Unsupported deployment schema.' }
$result = [ordered]@{ schemaVersion = 1; pythonReady = $false; wechatImportReady = $false; wechatVersion = $null; qqConfigured = $false; qqLoopbackWhitelist = $false; qqWebUiDisabled = $false; qqHealthReachable = $false; qqPluginOnline = $false; qqAuthenticated = $false; qqServerWhitelistVerified = $false; messageReadsPerformed = $false }
$python = [Environment]::ExpandEnvironmentVariables($config.pythonExe)
if (Test-Path -LiteralPath $python -PathType Leaf) {
    $probe = @'
import contextlib, importlib.metadata, io, json, os, sys
result = {"importReady": False, "version": None}
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        os.chdir(sys.argv[1])
        from wechatauto import WeChatDB, WxParam
        WxParam.ENABLE_FILE_LOGGER = False
        result["version"] = importlib.metadata.version("wechatauto-replica")
        result["importReady"] = callable(WeChatDB)
except Exception:
    pass
print(json.dumps(result))
'@
    try {
        $raw = @($probe | & $python - ([Environment]::ExpandEnvironmentVariables($config.privateRoot)) 2>$null)
        if ($LASTEXITCODE -eq 0) {
            $probeResult = ($raw -join "`n") | ConvertFrom-Json
            $result.pythonReady = $true
            $result.wechatImportReady = [bool]$probeResult.importReady
            $result.wechatVersion = $probeResult.version
        }
    } catch { }
}
if ($config.qqSecurityFile -and (Test-Path -LiteralPath ([Environment]::ExpandEnvironmentVariables($config.qqSecurityFile)) -PathType Leaf)) {
    try {
        $security = Get-Content -LiteralPath ([Environment]::ExpandEnvironmentVariables($config.qqSecurityFile)) -Raw | ConvertFrom-Json
        $result.qqConfigured = -not [string]::IsNullOrWhiteSpace($security.accessToken)
        $result.qqLoopbackWhitelist = @($security.allowedIPs).Count -eq 2 -and '127.0.0.1' -in @($security.allowedIPs) -and '::1' -in @($security.allowedIPs) -and
            @($security.allowedIPs | Where-Object { $_ -notin @('127.0.0.1', '::1') }).Count -eq 0 -and $security.disableIpWhitelist -eq $false
        $webui = Get-Content -LiteralPath (Join-Path ([Environment]::ExpandEnvironmentVariables($config.qqRoot)) 'config/webui.json') -Raw | ConvertFrom-Json
        $result.qqWebUiDisabled = $webui.disableWebUI -eq $true
    } catch { }
    if ($config.qqPort -eq 40653) {
        $base = 'http://127.0.0.1:40653'
        try {
            $health = Invoke-RestMethod -Uri ($base + '/health') -TimeoutSec 5
            $result.qqHealthReachable = $true
            $healthData = if ($null -ne $health.data) { $health.data } else { $health }
            $result.qqPluginOnline = $healthData.mode -eq 'plugin' -and $healthData.online -eq $true
        } catch { }
        if ($result.qqConfigured) {
            try {
                # This query only checks API authorization/whitelist; no contacts or messages.
                $response = Invoke-RestMethod -Uri ($base + '/api/security/ip-whitelist') -Headers @{ Authorization = 'Bearer ' + $security.accessToken } -TimeoutSec 5
                $result.qqAuthenticated = $response.success -eq $true
                $data = $response.data
                $result.qqServerWhitelistVerified = $result.qqAuthenticated -and @($data.allowedIPs).Count -eq 2 -and '127.0.0.1' -in @($data.allowedIPs) -and '::1' -in @($data.allowedIPs) -and
                    @($data.allowedIPs | Where-Object { $_ -notin @('127.0.0.1', '::1') }).Count -eq 0 -and $data.disabled -eq $false
            } catch { }
        }
    }
}
$result | ConvertTo-Json
