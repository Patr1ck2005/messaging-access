#Requires -Version 7.0
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ConfigPath = (Join-Path $env:LOCALAPPDATA 'MessagingAccess/deployment.json'),
    [string]$PythonExe,
    [uri]$PackageIndexUrl = 'https://pypi.org/simple',
    [string]$PrivateRoot = (Join-Path $env:LOCALAPPDATA 'MessagingAccess'),
    [string]$QQRoot,
    [string]$QQExe,
    [switch]$EnableQQ,
    [switch]$AcceptLanMetadataExposure,
    [switch]$SkipSkill,
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
if (-not $IsWindows) { throw 'This deployment requires Windows and PowerShell 7.' }
if (-not $PackageIndexUrl.IsAbsoluteUri -or $PackageIndexUrl.Scheme -ne 'https' -or $PackageIndexUrl.UserInfo) {
    throw '-PackageIndexUrl must be an absolute HTTPS package-index URL without embedded credentials.'
}
$repoRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
function FullPath([string]$Path) { [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path)) }
function ReadJson([string]$Path) {
    try { Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json -ErrorAction Stop }
    catch { throw "Cannot parse required JSON file: $([IO.Path]::GetFileName($Path))" }
}
function WriteNewJson([string]$Path, $Value) {
    # Atomic, no-replace rename prevents partial JSON and concurrent credential overwrite.
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.new'
    $stream = [IO.File]::Open($temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($Value | ConvertTo-Json -Depth 10))
        $stream.Write($bytes, 0, $bytes.Length)
    } finally { $stream.Dispose() }
    try { [IO.File]::Move($temporary, $Path) }
    catch {
        [IO.File]::Delete($temporary)
        throw 'JSON destination already exists or is not writable; no existing file was replaced.'
    }
}
function NewToken([int]$Length) {
    $alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
    -join (1..$Length | ForEach-Object { $alphabet[[Security.Cryptography.RandomNumberGenerator]::GetInt32($alphabet.Length)] })
}
function EnsurePrivateDirectory([string]$Path) {
    if (Test-Path -LiteralPath $Path -PathType Container) { return }
    $null = New-Item -ItemType Directory -Path $Path -Force
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    foreach ($sid in @($userSid, [Security.Principal.SecurityIdentifier]::new('S-1-5-18'),
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
        $rule = [Security.AccessControl.FileSystemAccessRule]::new($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function InitializeFreshWebui([string]$Path) {
    # Only call for files from THIS extraction, or a missing file. Never a live config.
    $webui = if (Test-Path -LiteralPath $Path) { ReadJson $Path } else { [pscustomobject]@{} }
    $settings = [ordered]@{
        host = '127.0.0.1'; port = 6099; token = NewToken 40; disableWebUI = $true
        accessControlMode = 'whitelist'; ipWhitelist = @('127.0.0.1', '::1'); enableXForwardedFor = $false
    }
    foreach ($entry in $settings.GetEnumerator()) {
        $webui | Add-Member -MemberType NoteProperty -Name $entry.Key -Value $entry.Value -Force
    }
    $temporary = $Path + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    WriteNewJson $temporary $webui
    [IO.File]::Move($temporary, $Path, $true)
}
function EnsureArtifact($Artifact, [string]$Directory) {
    $null = New-Item -ItemType Directory -Path $Directory -Force
    $path = Join-Path $Directory $Artifact.filename
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $temporary = $path + '.download'
        if (Test-Path -LiteralPath $temporary) { throw "Incomplete download exists; remove only that partial file before retrying: $temporary" }
        try {
            Invoke-WebRequest -Uri $Artifact.url -OutFile $temporary -TimeoutSec 1800
            if ((Get-FileHash -LiteralPath $temporary -Algorithm SHA256).Hash -ine $Artifact.sha256 -or
                (Get-Item -LiteralPath $temporary).Length -ne $Artifact.bytes) {
                throw "Downloaded artifact failed size/SHA256 verification: $($Artifact.filename)"
            }
            [IO.File]::Move($temporary, $path)
        } catch { throw "Official download failed; no unverified package was installed: $($Artifact.filename). An incomplete .download file may remain." }
    }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $Artifact.sha256 -or
        (Get-Item -LiteralPath $path).Length -ne $Artifact.bytes) {
        throw "Existing artifact failed size/SHA256 verification; it was not replaced: $($Artifact.filename)"
    }
    Write-Host "Verified upstream artifact: $($Artifact.filename)"
    $path
}
function FindQQ {
    $candidates = @(
        (Join-Path $env:ProgramFiles 'Tencent/QQNT/QQ.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Tencent/QQNT/QQ.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs/Tencent/QQNT/QQ.exe')
    )
    foreach ($candidate in $candidates) { if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate } }
    try {
        $registered = (Get-ItemProperty -LiteralPath 'Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\QQ.exe' -ErrorAction Stop).'(default)'
        if ($registered -and (Test-Path -LiteralPath $registered -PathType Leaf)) { return $registered }
    } catch { }
    return $null
}
if ($EnableQQ -and -not $AcceptLanMetadataExposure) {
    throw 'QQ v6.3.2 listens on all network interfaces. Static pages and limited health metadata can be LAN-visible; chat APIs require token plus loopback IP. Read the README and explicitly pass -AcceptLanMetadataExposure to enable QQ.'
}
$ConfigPath = FullPath $ConfigPath
$lock = ReadJson (Join-Path $repoRoot 'upstream-lock.json')
$configExists = Test-Path -LiteralPath $ConfigPath -PathType Leaf
$fillQQExe = $false
if ($configExists) {
    $deployment = ReadJson $ConfigPath
    if ($deployment.schemaVersion -ne 1 -or (FullPath $deployment.projectRoot) -ine $repoRoot) {
        throw 'Existing deployment config belongs to another schema/project directory. It was not overwritten; use a separate -ConfigPath or deliberately update the private config.'
    }
    foreach ($field in @('pythonExe', 'privateRoot', 'qqRoot', 'qqSecurityFile', 'qqPort')) {
        if ($null -eq $deployment.$field) { throw "Existing deployment config is missing $field; no overwrite was performed." }
    }
    if ($QQExe) {
        $requestedQQExe = FullPath $QQExe
        if ([IO.Path]::GetFileName($requestedQQExe) -ine 'QQ.exe' -or -not (Test-Path -LiteralPath $requestedQQExe -PathType Leaf)) {
            throw '-QQExe must identify an existing official QQ.exe.'
        }
        if ([string]::IsNullOrWhiteSpace($deployment.qqExe)) {
            $deployment | Add-Member -MemberType NoteProperty -Name qqExe -Value $requestedQQExe -Force
            $fillQQExe = $true
        } elseif ((FullPath $deployment.qqExe) -ine $requestedQQExe) {
            throw 'Existing qqExe conflicts with -QQExe. Edit the private deployment config consciously; no paths were changed.'
        }
    }
    Write-Host 'Reusing existing private deployment paths; only an explicitly supplied missing qqExe may be filled.'
} else {
    $PrivateRoot = FullPath $PrivateRoot
    if (-not $QQRoot) { $QQRoot = Join-Path $repoRoot ('vendor/qq/v' + $lock.qq.version) }
    $QQRoot = FullPath $QQRoot
    if ($QQExe) { $QQExe = FullPath $QQExe } else { $QQExe = FindQQ }
    $deployment = [pscustomobject][ordered]@{
        schemaVersion = 1
        projectRoot = $repoRoot
        pythonExe = Join-Path $repoRoot '.venv/Scripts/python.exe'
        privateRoot = $PrivateRoot
        qqRoot = $QQRoot
        qqSecurityFile = Join-Path (Split-Path $QQRoot -Parent) 'runtime/config/security.json'
        qqExe = $QQExe
        qqPort = 40653
    }
}
if ($EnableQQ -and (-not $deployment.qqExe -or [IO.Path]::GetFileName($deployment.qqExe) -ine 'QQ.exe' -or -not (Test-Path -LiteralPath (FullPath $deployment.qqExe) -PathType Leaf))) {
    throw 'QQNT was not found. Install official QQ yourself and pass -QQExe with its existing QQ.exe; this kit does not copy or download QQ client data.'
}
if (-not $PrepareOnly -and (FullPath $deployment.qqRoot) -ine (FullPath (Join-Path $repoRoot ('vendor/qq/v' + $lock.qq.version)))) {
    throw 'QQ binaries must use this project vendor/qq/v6.3.2 layout so checks and cache cleanup share one boundary. Custom -QQRoot is only available for isolated -PrepareOnly fixtures.'
}
if (-not $PSCmdlet.ShouldProcess($repoRoot, 'Prepare private deployment, install locked upstream packages and shared skill links')) { return }
EnsurePrivateDirectory (FullPath $deployment.privateRoot)
foreach ($directory in @((Split-Path $ConfigPath -Parent), $deployment.privateRoot,
    (Join-Path $deployment.privateRoot 'wechat'), (Join-Path $deployment.privateRoot 'wechat-keys'),
    (Join-Path $deployment.privateRoot 'media'))) {
    $null = New-Item -ItemType Directory -Path (FullPath $directory) -Force
}
if (-not $PrepareOnly) {
    $wechatWheel = EnsureArtifact $lock.wechat (Join-Path $repoRoot 'vendor/wechat')
    $venvPython = FullPath $deployment.pythonExe
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $venvRoot = Split-Path (Split-Path $venvPython -Parent) -Parent
        if (Test-Path -LiteralPath $venvRoot) { throw 'An incomplete .venv already exists; inspect it instead of overwriting it.' }
        if ($PythonExe) {
            $basePython = FullPath $PythonExe
            $version = & $basePython -c 'import sys; print("%s.%s" % sys.version_info[:2])'
            if ($LASTEXITCODE -ne 0 -or $version -ne '3.11') { throw '-PythonExe must select Python 3.11.' }
            & $basePython -m venv $venvRoot
        } else { & py -3.11 -m venv $venvRoot }
        if ($LASTEXITCODE -ne 0) { throw 'Could not create Python 3.11 virtual environment.' }
    }
    $venvVersion = & $venvPython -c 'import sys; print("%s.%s" % sys.version_info[:2])'
    if ($LASTEXITCODE -ne 0 -or $venvVersion -ne '3.11') { throw 'The deployment virtual environment must use Python 3.11.' }
    & $venvPython -m pip install --disable-pip-version-check --index-url $PackageIndexUrl.AbsoluteUri $wechatWheel -r (Join-Path $repoRoot 'requirements.lock')
    if ($LASTEXITCODE -ne 0) { throw 'Locked dependency installation failed.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Installed Python dependencies failed pip check.' }
}
if ($EnableQQ) {
    $qqRootPath = FullPath $deployment.qqRoot
    $qqConfigPath = Join-Path $qqRootPath 'config'
    $qqRuntime = Split-Path (Split-Path (FullPath $deployment.qqSecurityFile) -Parent) -Parent
    $newExtraction = $false
    if (-not $PrepareOnly) {
        $archive = EnsureArtifact $lock.qq (Join-Path $repoRoot 'vendor/qq')
        if (-not (Test-Path -LiteralPath $qqRootPath)) {
            Expand-Archive -LiteralPath $archive -DestinationPath $qqRootPath
            $newExtraction = $true
        }
        foreach ($required in @('napiLoader.bat', 'napimain.exe', 'napiloader.dll', 'nativeLoader.cjs')) {
            if (-not (Test-Path -LiteralPath (Join-Path $qqRootPath $required) -PathType Leaf)) {
                throw "QQ extraction is incomplete: $required. Existing files were not overwritten."
            }
        }
    }
    EnsurePrivateDirectory $qqRuntime
    EnsurePrivateDirectory (Split-Path (FullPath $deployment.qqSecurityFile) -Parent)
    foreach ($directory in @($qqConfigPath, (Join-Path $qqRuntime 'logs'))) {
        $null = New-Item -ItemType Directory -Path $directory -Force
    }
    $securityPath = FullPath $deployment.qqSecurityFile
    if (-not (Test-Path -LiteralPath $securityPath)) {
        WriteNewJson $securityPath ([ordered]@{
            accessToken = NewToken 40; secretKey = NewToken 64
            createdAt = [DateTimeOffset]::UtcNow.ToString('o')
            tokenExpired = [DateTimeOffset]::UtcNow.AddDays(7).ToString('o')
            allowedIPs = @('127.0.0.1', '::1'); disableIpWhitelist = $false; serverHost = '127.0.0.1'
        })
    } else { Write-Host 'Existing QQ security config preserved.' }
    # Never rewrite an existing runtime config, even while QQ is stopped.
    $webuiPath = Join-Path $qqConfigPath 'webui.json'
    if ($newExtraction -or -not (Test-Path -LiteralPath $webuiPath)) {
        InitializeFreshWebui $webuiPath
    }
    $napcatPath = Join-Path $qqConfigPath 'napcat.json'
    if ($newExtraction -and (Test-Path -LiteralPath $napcatPath)) {
        # This file came from the verified archive in this invocation, never a live instance.
        $napcat = ReadJson $napcatPath
        $napcat.fileLogLevel = 'warn'
        $napcat.consoleLogLevel = 'warn'
        [IO.File]::WriteAllText($napcatPath, ($napcat | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    }
    if (-not (Test-Path -LiteralPath $napcatPath)) {
        WriteNewJson $napcatPath ([ordered]@{
            fileLog = $true; consoleLog = $true; fileLogLevel = 'warn'; consoleLogLevel = 'warn'
            packetBackend = 'auto'; packetServer = ''; o3HookMode = 1
        })
    }
    Write-Warning 'QQ HTTP listens on 0.0.0.0. Existing configs were preserved; run scripts/check.ps1 before starting. No QQ process was started or stopped.'
}
if (-not $configExists) { WriteNewJson $ConfigPath $deployment }
elseif ($fillQQExe) {
    $temporaryConfig = $ConfigPath + '.' + [guid]::NewGuid().ToString('N') + '.tmp'
    WriteNewJson $temporaryConfig $deployment
    [IO.File]::Move($temporaryConfig, $ConfigPath, $true)
    Write-Host 'Filled only the previously missing qqExe in private deployment config.'
}
if (-not $SkipSkill) { & (Join-Path $PSScriptRoot 'install-skill.ps1') }
Write-Output ([pscustomobject]@{ configReady = $true; preparedOnly = [bool]$PrepareOnly; qqRequested = [bool]$EnableQQ; autoStarted = $false })
