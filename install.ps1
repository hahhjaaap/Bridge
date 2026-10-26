# UDB Windows x64 installer. No elevation, Python, Node.js or other bridge required.
[CmdletBinding()]
param(
    [string]$FeedUrl = 'https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest/download',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\UDB'),
    [string]$DataHome = (Join-Path $env:LOCALAPPDATA 'UniversalDesktopBridge'),
    [switch]$NoStart,
    [switch]$NoPath,
    [switch]$AllowLocalFeed
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($env:OS -ne 'Windows_NT') { throw 'This installer requires Windows.' }
$architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
if ($architecture -ne 'AMD64') { throw 'This release requires Windows x64.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
$feed = [Uri]$FeedUrl
if (!$feed.IsAbsoluteUri -or $feed.UserInfo -or $feed.Query -or $feed.Fragment -or ($feed.Scheme -ne 'https' -and !($AllowLocalFeed -and $feed.IsLoopback -and $feed.Scheme -eq 'http'))) { throw 'Use an HTTPS release feed.' }

function Assert-LocalPath([string]$Path) {
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith('\\') -or $full.Substring(2).Contains(':') -or $full -eq [IO.Path]::GetPathRoot($full)) { throw 'Select a local application folder.' }
    $cursor = $full
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -Force -LiteralPath $cursor
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Redirected installation paths are refused.' }
        }
        $parent = [IO.Path]::GetDirectoryName($cursor)
        if ($parent -eq $cursor) { break }
        $cursor = $parent
    }
    return $full
}
function Write-Utf8([string]$Path,[string]$Text) { [IO.File]::WriteAllText($Path,$Text,[Text.UTF8Encoding]::new($false)) }
function Get-Sha([string]$Path) { return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant() }
$InstallRoot = Assert-LocalPath $InstallRoot
$DataHome = Assert-LocalPath $DataHome
$recordPath = Join-Path $InstallRoot 'install.json'
$previous = $null
if (Test-Path -LiteralPath $InstallRoot) {
    $items = @(Get-ChildItem -Force -LiteralPath $InstallRoot)
    if ($items.Count -and !(Test-Path -LiteralPath $recordPath -PathType Leaf)) { throw 'The destination contains unrelated files; select an empty folder.' }
    if (Test-Path -LiteralPath $recordPath) {
        $previous = Get-Content -Raw -LiteralPath $recordPath | ConvertFrom-Json
        if ($previous.product -ne 'UniversalDesktopBridge' -or $previous.schema -ne 1) { throw 'Unrecognized installation; nothing was replaced.' }
        if (!$PSBoundParameters.ContainsKey('DataHome')) { $DataHome = Assert-LocalPath ([string]$previous.home) }
    }
}
$mutexName = 'Local\UDB-Install-' + ([BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($InstallRoot.ToLowerInvariant())))).Replace('-','').Substring(0,24)
$mutex = [Threading.Mutex]::new($false,$mutexName)
$locked = $false
$temp = $null
$stage = $null
try {
    $locked = $mutex.WaitOne(0)
    if (!$locked) { throw 'Another UDB installer is running.' }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('udb-install-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $archive = Join-Path $temp 'udb-windows-x64.zip'
    Write-Host 'Downloading the standalone UDB application...'
    $base = $feed.AbsoluteUri.TrimEnd('/')
    Invoke-WebRequest -UseBasicParsing -Uri ($base + '/SHA256SUMS') -OutFile (Join-Path $temp 'SHA256SUMS') -TimeoutSec 60
    $checksumText = [IO.File]::ReadAllText((Join-Path $temp 'SHA256SUMS'))
    $matchesHash = [regex]::Matches($checksumText,'(?m)^([a-fA-F0-9]{64})\s+udb-windows-x64\.zip\s*$')
    if ($matchesHash.Count -ne 1) { throw 'Release checksum is missing or ambiguous.' }
    $expected = $matchesHash[0].Groups[1].Value.ToLowerInvariant()
    Invoke-WebRequest -UseBasicParsing -Uri ($base + '/udb-windows-x64.zip') -OutFile $archive -TimeoutSec 600
    if ((Get-Item -LiteralPath $archive).Length -gt 536870912 -or (Get-Sha $archive) -ne $expected) { throw 'Download verification failed; nothing was installed.' }
    [IO.Directory]::CreateDirectory($InstallRoot) | Out-Null
    $stage = Join-Path $InstallRoot ('.stage-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($stage) | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $zip = [IO.Compression.ZipFile]::OpenRead($archive)
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    try {
        if ($zip.Entries.Count -gt 5000) { throw 'Archive has too many entries.' }
        [long]$total = 0
        foreach ($entry in $zip.Entries) {
            $name = $entry.FullName
            if (!$name.StartsWith('UDB/') -or $name.Contains('\') -or $name.Contains(':') -or $name -match '(^|/)(\.|\.\.|)(/|$)' -or !$names.Add($name) -or (($entry.ExternalAttributes -shr 16) -band 61440) -eq 40960) { throw 'Unsafe archive entry.' }
            $total += $entry.Length
            if ($total -gt 1073741824 -or $entry.Length -gt 536870912) { throw 'Expanded archive exceeds limits.' }
            $relative = $name.Substring(4)
            if (!$relative) { throw 'Empty archive entry.' }
            $destination = [IO.Path]::GetFullPath((Join-Path $stage $relative))
            if (!$destination.StartsWith($stage + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Archive escaped its stage.' }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            $inputStream = $entry.Open()
            $outputStream = [IO.File]::Open($destination,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            try {
                $buffer = New-Object byte[] 65536
                [long]$written = 0
                while (($count = $inputStream.Read($buffer,0,$buffer.Length)) -gt 0) {
                    $written += $count
                    if ($written -gt $entry.Length) { throw 'Expanded entry size mismatch.' }
                    $outputStream.Write($buffer,0,$count)
                }
                if ($written -ne $entry.Length) { throw 'Truncated archive entry.' }
            } finally { $outputStream.Dispose(); $inputStream.Dispose() }
        }
    } finally { $zip.Dispose() }
    $manifest = Join-Path $stage 'STANDALONE-SHA256SUMS'
    $inventory = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    [void]$inventory.Add('STANDALONE-SHA256SUMS')
    foreach ($line in [IO.File]::ReadAllLines($manifest)) {
        if ($line -notmatch '^([a-fA-F0-9]{64})  (.+)$') { throw 'Invalid package inventory.' }
        $digest = $Matches[1].ToLowerInvariant(); $name = $Matches[2]
        if ($name.Contains('\') -or $name.Contains(':') -or $name -match '(^|/)(\.|\.\.|)(/|$)') { throw 'Unsafe inventory path.' }
        $file = [IO.Path]::GetFullPath((Join-Path $stage $name))
        if (!$file.StartsWith($stage + '\',[StringComparison]::OrdinalIgnoreCase) -or (Get-Sha $file) -ne $digest) { throw 'Package inventory mismatch.' }
        [void]$inventory.Add($name)
    }
    if (!$inventory.Contains('udb.exe')) { throw 'The application is missing.' }
    if ($names.Count -ne $inventory.Count) { throw 'Unexpected package files.' }
    foreach ($name in $inventory) { if (!$names.Contains('UDB/' + $name)) { throw 'Missing package file.' } }
    $candidate = Join-Path $stage 'udb.exe'
    $releases = Join-Path $InstallRoot 'releases'
    [IO.Directory]::CreateDirectory($releases) | Out-Null
    $release = Join-Path $releases $expected.Substring(0,24)
    if (Test-Path -LiteralPath $release) {
        if ((Get-Sha (Join-Path $release 'udb.exe')) -ne (Get-Sha $candidate)) { throw 'Existing release differs; preserved it.' }
    } else { [IO.Directory]::Move($stage,$release); $stage = $null }
    $exe = Join-Path $release 'udb.exe'
    # Probe only at the final path. Windows can retain an image mapping after
    # a onefile bootloader exits; never execute an image before renaming it.
    $version = (@(& $exe --version) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Application verification failed.' }
    if ($previous -and $previous.exe -ne $exe) {
        $old = Assert-LocalPath ([string]$previous.exe)
        if (!$old.StartsWith($releases + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid previous release path.' }
        Write-Host 'Stopping the previous UDB version for the update...'
        $stopped = (@(& $old --home $DataHome stop --wait 60 --json) -join "`n") | ConvertFrom-Json
        if ($stopped.manager_running) { throw 'The old agent did not stop; active installation preserved.' }
    }
    $bin = Join-Path $InstallRoot 'bin'
    [IO.Directory]::CreateDirectory($bin) | Out-Null
    $launcher = @'
$ErrorActionPreference = 'Stop'
try {
    $record = Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot '..\install.json') | ConvertFrom-Json
    if ($record.product -ne 'UniversalDesktopBridge' -or $record.schema -ne 1) { throw 'Invalid install record' }
    $forward = @($args)
    if (!$forward.Count) { $forward = @('start') }
    if ($forward[0] -eq 'start' -and ![Console]::IsInputRedirected -and ![Console]::IsOutputRedirected -and !(@($forward | Where-Object { $_ -match '^--(json|detach|follow)(=|$)' }).Count)) { $forward += '--follow' }
    $env:PYTHONIOENCODING = 'utf-8'
    [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
    $OutputEncoding = [Console]::OutputEncoding
    & $record.exe --home $record.home @forward
    exit $LASTEXITCODE
} catch { [Console]::Error.WriteLine('UDB launcher failed. Re-run the installer to repair it.'); exit 1 }
'@
    Write-Utf8 (Join-Path $bin 'udb.ps1') $launcher
    Write-Utf8 (Join-Path $bin 'udb.cmd') "@echo off`r`n`"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"%~dp0udb.ps1`" %*`r`nexit /b %errorlevel%`r`n"
    $record = @{product='UniversalDesktopBridge';schema=1;version=$version;exe=$exe;home=$DataHome;archive_sha256=$expected}
    $newRecord = $recordPath + '.new'
    Write-Utf8 $newRecord ($record | ConvertTo-Json -Compress)
    if (Test-Path -LiteralPath $recordPath) {
        $backupRecord = $recordPath + '.previous-' + [Guid]::NewGuid().ToString('N')
        [IO.File]::Replace($newRecord,$recordPath,$backupRecord)
        Remove-Item -LiteralPath $backupRecord -Force
    } else { [IO.File]::Move($newRecord,$recordPath) }
    $mcp = @(& $exe --home $DataHome mcp-config)
    if ($LASTEXITCODE -ne 0) { throw 'Cannot generate MCP configuration.' }
    Write-Utf8 (Join-Path $InstallRoot 'mcp.json') ($mcp -join "`n")
    if (!$NoPath) {
        $oldPath = [Environment]::GetEnvironmentVariable('Path','User')
        $parts = @($oldPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $bin.TrimEnd('\') })
        [Environment]::SetEnvironmentVariable('Path',(@($bin) + $parts -join ';'),'User')
        $env:Path = $bin + ';' + $env:Path
    }
    Write-Host ('UDB ' + $version + ' installed. Command: udb') -ForegroundColor Green
    Write-Host ('Local MCP configuration: ' + (Join-Path $InstallRoot 'mcp.json'))
    if (!$NoStart) {
        $env:PYTHONIOENCODING = 'utf-8'
        [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
        $OutputEncoding = [Console]::OutputEncoding
        & $exe --home $DataHome setup
        if ($LASTEXITCODE -ne 0) { throw 'Installation succeeded; first-run setup is incomplete. Run udb setup locally.' }
    }
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) { Remove-Item -LiteralPath $stage -Recurse -Force }
    if ($temp -and (Test-Path -LiteralPath $temp)) { Remove-Item -LiteralPath $temp -Recurse -Force }
    if ($locked) { $mutex.ReleaseMutex() }
    $mutex.Dispose()
}
