# Install the UDB one-command companion. Uses the current Windows user only.
[CmdletBinding()]
param(
    [string]$BridgeHome = (Join-Path $env:USERPROFILE 'Downloads\Universal-Desktop-Bridge'),
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
function Assert-NoLinks([string]$Path) {
    $node = [IO.FileInfo]::new([IO.Path]::GetFullPath($Path))
    if ((Test-Path -LiteralPath $Path) -and
        ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked path refused.' }
    $parent = [IO.DirectoryInfo]::new([IO.Path]::GetDirectoryName($node.FullName))
    while ($null -ne $parent) {
        if ($parent.Exists -and ($parent.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Linked path refused.' }
        $parent = $parent.Parent
    }
}
$BridgeHome = [IO.Path]::GetFullPath($BridgeHome).TrimEnd('\')
Assert-NoLinks $BridgeHome
$meta = $null
$runtimeDir = $BridgeHome
$metadataPath = Join-Path $BridgeHome 'installation.json'
if (Test-Path -LiteralPath $metadataPath) {
    Assert-NoLinks $metadataPath
    $meta = [IO.File]::ReadAllText($metadataPath) | ConvertFrom-Json
    if ($meta.active -notmatch '^[0-9A-Za-z.-]{1,90}$' -or $meta.active -in @('.', '..') -or
        $meta.sha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid installed release metadata.' }
    $runtimeDir = Join-Path $BridgeHome ('releases\' + $meta.active)
}
$exe = Join-Path $runtimeDir 'UniversalDesktopBridge.exe'
$pythonExe = Join-Path $runtimeDir 'vendor\python-runtime\python.exe'
foreach ($file in @($exe, $pythonExe)) {
    Assert-NoLinks $file
    if (!(Test-Path -LiteralPath $file -PathType Leaf)) { throw "Existing UDB runtime missing: $file" }
}
if ($meta -and (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -ine $meta.sha256) {
    throw 'Installed EXE integrity check failed.'
}
$commandDir = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps'
$commandPath = Join-Path $commandDir 'udb.cmd'
$wrapperPath = Join-Path $commandDir 'udb-command.ps1'
$configPath = Join-Path $commandDir 'udb-command.json'
$companionDir = Join-Path $BridgeHome 'UDB-Command'
$marker = 'Universal Desktop Bridge one-command'
foreach ($item in @(Get-Command udb -All -ErrorAction SilentlyContinue)) {
    if ($item.Source -ine $commandPath) { throw "Another udb command exists: $($item.Source)" }
}
foreach ($file in @($commandPath, $wrapperPath)) {
    Assert-NoLinks $file
    if ((Test-Path -LiteralPath $file) -and !([IO.File]::ReadAllText($file).Contains($marker))) {
        throw "Refusing to overwrite an unrelated launcher: $file"
    }
}
Assert-NoLinks $configPath
if (Test-Path -LiteralPath $configPath) {
    $old = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
    if ($old.owner -cne 'udb-one-command-v2' -or
        [IO.Path]::GetFullPath([string]$old.bridge_home).TrimEnd('\') -ine $BridgeHome) {
        throw 'udb already targets another installation. Use that installation or choose a different command explicitly.'
    }
}
Assert-NoLinks $companionDir
if (Test-Path -LiteralPath $companionDir) {
    $owned = Join-Path $companionDir 'owner.txt'
    Assert-NoLinks $owned
    if (!(Test-Path -LiteralPath $owned) -or [IO.File]::ReadAllText($owned).Trim() -cne 'udb-one-command-v2') {
        throw 'Refusing to replace an unrelated companion directory.'
    }
}
$pathEntries = @($env:Path.Split(';') | ForEach-Object {
    [Environment]::ExpandEnvironmentVariables($_).Trim().Trim('"').TrimEnd('\')
})
if ($pathEntries -inotcontains $commandDir.TrimEnd('\')) {
    throw 'The user WindowsApps directory is absent from this terminal PATH. Open a terminal with the standard user PATH, then retry.'
}
# Verify distributed companion components before touching the installed launcher.
$sourceDir = Join-Path $PSScriptRoot 'command'
$manifestPath = Join-Path $sourceDir 'MANIFEST.json'
$manifest = [IO.File]::ReadAllText($manifestPath) | ConvertFrom-Json
foreach ($name in @('udb_cli.py', 'udb-command.ps1')) {
    $source = Join-Path $sourceDir $name
    Assert-NoLinks $source
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash -ine $manifest.$name) {
        throw "Companion checksum mismatch: $name"
    }
}
$utf8 = New-Object Text.UTF8Encoding($false)
$utf8Bom = New-Object Text.UTF8Encoding($true)
[IO.Directory]::CreateDirectory($commandDir) > $null
[IO.Directory]::CreateDirectory($companionDir) > $null
$entry = Join-Path $companionDir 'udb_cli.py'
Assert-NoLinks $entry
# Verify Python can execute the companion before switching the command files.
$stage = Join-Path $companionDir ('candidate-' + [Guid]::NewGuid().ToString('N') + '.py')
try {
    Copy-Item -LiteralPath (Join-Path $sourceDir 'udb_cli.py') -Destination $stage
    $version = & $pythonExe -I $stage --version
    if ($LASTEXITCODE -ne 0 -or $version -ne '4.1.0') { throw 'Bundled runtime companion check failed.' }
    Assert-NoLinks ($entry + '.previous')
    if (Test-Path -LiteralPath $entry) { Copy-Item -LiteralPath $entry -Destination ($entry + '.previous') -Force }
    Move-Item -LiteralPath $stage -Destination $entry -Force
} finally {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage }
}
[IO.File]::WriteAllText((Join-Path $companionDir 'owner.txt'), 'udb-one-command-v2', $utf8)
$writes = @(
    @{Path=$configPath; Text=(@{owner='udb-one-command-v2';bridge_home=$BridgeHome} | ConvertTo-Json); Encoding=$utf8},
    @{Path=$wrapperPath; Text=[IO.File]::ReadAllText((Join-Path $sourceDir 'udb-command.ps1')); Encoding=$utf8Bom},
    @{Path=$commandPath; Text=(@('@echo off', ('rem ' + $marker + ' companion v2'),
        '"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0udb-command.ps1" %*',
        'exit /b %errorlevel%', '') -join "`r`n"); Encoding=$utf8}
)
foreach ($write in $writes) {
    if (Test-Path -LiteralPath $write.Path) {
        Assert-NoLinks ($write.Path + '.udb-previous')
        Copy-Item -LiteralPath $write.Path -Destination ($write.Path + '.udb-previous') -Force
    }
    $temp = $write.Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    try {
        [IO.File]::WriteAllText($temp, $write.Text, $write.Encoding)
        Move-Item -LiteralPath $temp -Destination $write.Path -Force
    } finally {
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp }
    }
}
Write-Output 'Installed companion v4.1: udb | udb --access computer | udb revoke-access | udb stop | udb test'
if (!$NoLaunch) {
    & $commandPath
    exit $LASTEXITCODE
}
