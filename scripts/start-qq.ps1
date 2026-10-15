#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param([string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'MessagingAccess/deployment.json'))
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This deployment requires Windows and PowerShell 7.' }
try { $config = Get-Content -LiteralPath ([Environment]::ExpandEnvironmentVariables($ConfigPath)) -Raw | ConvertFrom-Json }
catch { throw 'Private deployment config is unavailable or invalid. Run install.ps1 first.' }
if ($config.schemaVersion -ne 1 -or $config.qqPort -ne 40653) { throw 'Unsupported deployment schema or QQ port.' }
$qqRoot = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($config.qqRoot))
$qqExe = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($config.qqExe))
$securityPath = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($config.qqSecurityFile))
$loader = Join-Path $qqRoot 'napiLoader.bat'
foreach ($path in @($loader, $qqExe, $securityPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'QQ launcher, QQ.exe or private security config is missing.' }
    if ($path -match '[!%"\r\n&|<>^]') { throw 'QQ launcher paths contain unsupported cmd.exe metacharacters. Move this deployment to a simple path.' }
}
try {
    $security = Get-Content -LiteralPath $securityPath -Raw | ConvertFrom-Json
    $localWhitelist = @($security.allowedIPs).Count -eq 2 -and '127.0.0.1' -in @($security.allowedIPs) -and '::1' -in @($security.allowedIPs) -and
        @($security.allowedIPs | Where-Object { $_ -notin @('127.0.0.1', '::1') }).Count -eq 0 -and
        $security.disableIpWhitelist -eq $false -and -not [string]::IsNullOrWhiteSpace($security.accessToken)
} catch { throw 'QQ security config is invalid; no private details are shown.' }
if (-not $localWhitelist) { throw 'Expected a token and exactly the loopback IP whitelist with enforcement enabled. No config was changed.' }
$webuiPath = Join-Path $qqRoot 'config/webui.json'
try { $webui = Get-Content -LiteralPath $webuiPath -Raw | ConvertFrom-Json }
catch { throw 'NapCat WebUI config is missing/invalid; no instance was started.' }
if ($webui.disableWebUI -ne $true) { throw 'NapCat WebUI must be disabled for this deployment.' }
if (Get-Process -Name QQ -ErrorAction SilentlyContinue) { throw 'Save drafts and normally exit QQ completely from its tray first. This script never force-stops QQ.' }
foreach ($port in @(40653, 40654)) {
    if (Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue) { throw "Port $port is occupied. Inspect the existing instance; no second process was started." }
}
if (-not $PSCmdlet.ShouldProcess($qqExe, 'Start existing QQ through the official QCE Framework launcher')) { return }
$runtime = Split-Path (Split-Path $securityPath -Parent) -Parent
$logs = Join-Path $runtime 'logs'
$null = New-Item -ItemType Directory -Path $logs -Force
$environmentNames = @('QCE_CONFIG_DIR', 'QCE_LOG_DIR', 'QCE_NO_AUTO_OPEN', 'QCE_STANDALONE_MODE', 'QCE_SERVER_PORT', 'RUST_LOG')
$previousEnvironment = @{}
foreach ($name in $environmentNames) { $previousEnvironment[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
try {
    $env:QCE_CONFIG_DIR = Split-Path $securityPath -Parent
    $env:QCE_LOG_DIR = $logs
    $env:QCE_NO_AUTO_OPEN = '1'
    $env:QCE_SERVER_PORT = '40653'
    $env:RUST_LOG = 'warn'
    Remove-Item Env:QCE_STANDALONE_MODE -ErrorAction SilentlyContinue
    $command = '""' + $loader + '" "' + $qqExe + '""'
    $process = Start-Process -FilePath $env:ComSpec -ArgumentList @('/d', '/s', '/c', $command) -WorkingDirectory $qqRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'framework-launch-out.log') -RedirectStandardError (Join-Path $logs 'framework-launch-err.log') -PassThru
    Write-Output ([pscustomobject]@{ launcherStarted = $true; launcherPid = $process.Id; loginMayRequireUser = $true })
} finally {
    foreach ($name in $environmentNames) { [Environment]::SetEnvironmentVariable($name, $previousEnvironment[$name], 'Process') }
}
