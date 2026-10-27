# UDB Windows x64 installer. No elevation, Python, Node.js or other bridge required.
[CmdletBinding()]
param(
    [string]$FeedUrl = 'https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest/download',
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA 'Programs\UDB'),
    [string]$DataHome = (Join-Path $env:LOCALAPPDATA 'UniversalDesktopBridge'),
    [switch]$NoStart,
    [switch]$NoPath,
    [switch]$Rollback,
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
function Get-Sha([string]$Path) {
    $hash = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try { return ([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','').ToLowerInvariant() }
    finally { $stream.Dispose(); $hash.Dispose() }
}
function Write-AtomicText([string]$Path,[string]$Text) {
    [void](Assert-LocalPath $Path)
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.new'
    try {
        $stream = [IO.File]::Open($temporary,[IO.FileMode]::CreateNew)
        try { $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Text); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary,$Path,$null) } else { [IO.File]::Move($temporary,$Path) }
    } finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
}
function Get-InstallRecord([string]$Path) {
    [void](Assert-LocalPath $Path)
    if ((Get-Item -LiteralPath $Path).Length -gt 8192) { throw 'Installation record exceeds limits.' }
    $value = Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json
    return $value
}
function Assert-Release($Record) {
    if ($Record.product -ne 'UniversalDesktopBridge' -or $Record.schema -ne 1 -or $Record.archive_sha256 -cnotmatch '^[a-f0-9]{64}$' -or $Record.version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release record.' }
    $image = Assert-LocalPath ([string]$Record.exe)
    $expectedImage = Join-Path (Join-Path (Join-Path $InstallRoot 'releases') $Record.archive_sha256.Substring(0,24)) 'udb.exe'
    if ($image -ine $expectedImage -or !(Test-Path -LiteralPath $image -PathType Leaf) -or (Assert-LocalPath ([string]$Record.home)) -ine $DataHome) { throw 'Release is outside this installation or uses another data home.' }
    $directory = [IO.Path]::GetDirectoryName($image)
    $manifestPath = Assert-LocalPath (Join-Path $directory 'STANDALONE-SHA256SUMS')
    if ((Get-Item -LiteralPath $manifestPath).Length -gt 1048576) { throw 'Release inventory exceeds limits.' }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($line in [IO.File]::ReadAllLines($manifestPath)) {
        if ($line -notmatch '^([a-fA-F0-9]{64})  (.+)$') { throw 'Invalid retained release inventory.' }
        $digest = $Matches[1]; $name = $Matches[2]
        if ($name.Contains('\') -or $name.Contains(':') -or $name -match '(^|/)(\.|\.\.|)(/|$)' -or !$seen.Add($name)) { throw 'Unsafe retained release path.' }
        $file = Assert-LocalPath (Join-Path $directory $name)
        if (!$file.StartsWith($directory + '\',[StringComparison]::OrdinalIgnoreCase) -or (Get-Sha $file) -ne $digest) { throw 'Retained release integrity failed.' }
    }
    if (!$seen.Contains('udb.exe')) { throw 'Retained release has no application.' }
}
function Get-AgentState([string]$Image) {
    $text = @(& $Image --home $DataHome status --json)
    $state = ($text -join "`n") | ConvertFrom-Json
    if ($state.PSObject.Properties.Name -notcontains 'manager_running') { throw 'Cannot inspect the existing agent.' }
    return $state
}
function Start-Agent([string]$Image,[string]$Mode) {
    $options = @('--home',$DataHome,'start','--detach','--json','--wait','60')
    if ($Mode -eq 'offline') { $options += '--offline' }
    elseif ((@(& $Image start --help) -join ' ') -match '--online') { $options += '--online' }
    $state = (@(& $Image @options) -join "`n") | ConvertFrom-Json
    if (!$state.manager_running -or $state.connection_mode -ne $Mode) { throw 'New agent failed to start in the previous connection mode.' }
    # A temporary network outage is not a broken application. The agent retries.
}
function Set-ActiveRelease($Record,$Old) {
    Assert-Release $Record
    $mcpPath = Assert-LocalPath (Join-Path $InstallRoot 'mcp.json')
    if ((Test-Path -LiteralPath $mcpPath) -and (Get-Item -LiteralPath $mcpPath).Length -gt 65536) { throw 'Existing MCP configuration exceeds limits.' }
    $oldMcp = if (Test-Path -LiteralPath $mcpPath) { [IO.File]::ReadAllText($mcpPath) } else { $null }
    $oldState = $null
    $switched = $false
    $stopped = $false
    try {
        # Generate config before interrupting an existing agent.
        $mcp = @(& $Record.exe --home $DataHome mcp-config)
        if ($LASTEXITCODE -ne 0) { throw 'Cannot generate MCP configuration.' }
        [void](($mcp -join "`n") | ConvertFrom-Json)
        if ($Old -and $Old.exe -ne $Record.exe) {
            Assert-Release $Old
            $oldState = Get-AgentState $Old.exe
            if ($oldState.manager_running) {
                $stopState = (@(& $Old.exe --home $DataHome stop --wait 60 --json) -join "`n") | ConvertFrom-Json
                if ($stopState.manager_running -or $LASTEXITCODE -ne 0) { throw 'The previous agent did not stop.' }
                $stopped = $true
            }
        }
        Write-AtomicText $mcpPath ($mcp -join "`n")
        Write-AtomicText $recordPath ($Record | ConvertTo-Json -Depth 5 -Compress)
        $switched = $true
        if ($oldState -and $oldState.manager_running) { Start-Agent $Record.exe $oldState.connection_mode }
    } catch {
        if ($switched) {
            try { & $Record.exe --home $DataHome stop --wait 60 --json | Out-Null } catch { }
        }
        if ($Old) {
            Write-AtomicText $recordPath ($Old | ConvertTo-Json -Depth 5 -Compress)
            if ($null -ne $oldMcp) { Write-AtomicText $mcpPath $oldMcp }
            if ($stopped) {
                try { Start-Agent $Old.exe $oldState.connection_mode } catch { Write-Warning 'Previous release restored; run udb start to reconnect.' }
            }
        } else {
            if ($switched -and (Test-Path -LiteralPath $recordPath)) { Remove-Item -LiteralPath $recordPath -Force }
            if ($null -eq $oldMcp -and (Test-Path -LiteralPath $mcpPath)) { Remove-Item -LiteralPath $mcpPath -Force }
        }
        throw 'Activation failed; previous installation retained. Run udb doctor.'
    }
}
function Get-Download([string]$Url,[string]$Destination,[int]$Timeout) {
    for ($attempt=0; $attempt -lt 3; $attempt++) {
        try { Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $Destination -TimeoutSec $Timeout; return }
        catch {
            if (Test-Path -LiteralPath $Destination) { Remove-Item -LiteralPath $Destination -Force }
            $response = $_.Exception.Response
            $code = if ($response) { [int]$response.StatusCode } else { 0 }
            if ($attempt -eq 2 -or ($code -ne 0 -and $code -ne 408 -and $code -ne 429 -and $code -lt 500)) { throw 'Release download failed. Check the network and retry; active installation is unchanged.' }
            Start-Sleep -Seconds ($attempt + 1)
        }
    }
}
$InstallRoot = Assert-LocalPath $InstallRoot
$DataHome = Assert-LocalPath $DataHome
if ($InstallRoot -ieq $DataHome -or $DataHome.StartsWith($InstallRoot + '\',[StringComparison]::OrdinalIgnoreCase) -or $InstallRoot.StartsWith($DataHome + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Choose separate application and data folders.' }
$recordPath = Join-Path $InstallRoot 'install.json'
$previous = $null
if (Test-Path -LiteralPath $InstallRoot) {
    $items = @(Get-ChildItem -Force -LiteralPath $InstallRoot)
    if ($items.Count -and !(Test-Path -LiteralPath $recordPath -PathType Leaf)) { throw 'The destination contains unrelated files; select an empty folder.' }
    if (Test-Path -LiteralPath $recordPath) {
        $previous = Get-InstallRecord $recordPath
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
    # Refresh after taking the lease: another completed installer may have switched it.
    if (Test-Path -LiteralPath $recordPath) { $previous = Get-InstallRecord $recordPath }
    if ($Rollback) {
        if (!$previous -or !$previous.previous) { throw 'No previous release is available.' }
        $target = $previous.previous
        $record = @{product='UniversalDesktopBridge';schema=1;version=$target.version;exe=$target.exe;home=$target.home;archive_sha256=$target.archive_sha256;previous=@{product=$previous.product;schema=$previous.schema;version=$previous.version;exe=$previous.exe;home=$previous.home;archive_sha256=$previous.archive_sha256}}
        Set-ActiveRelease $record $previous
        Write-Host ('UDB rolled back to ' + $target.version + '. Settings preserved.')
        return
    }
    $temp = Join-Path ([IO.Path]::GetTempPath()) ('udb-install-' + [Guid]::NewGuid().ToString('N'))
    [IO.Directory]::CreateDirectory($temp) | Out-Null
    $archive = Join-Path $temp 'udb-windows-x64.zip'
    Write-Host 'Downloading the standalone UDB application...'
    $base = $feed.AbsoluteUri.TrimEnd('/')
    if ($base -eq 'https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/latest/download') {
        $latest = Invoke-RestMethod -Uri 'https://api.github.com/repos/hahhjaaap/Universal-Desktop-Bridge/releases/latest' -Headers @{'User-Agent'='UDB-installer'} -TimeoutSec 30
        if ($latest.tag_name -notmatch '^v\d+\.\d+\.\d+$' -or $latest.draft -or $latest.prerelease) { throw 'Invalid release metadata.' }
        $base = 'https://github.com/hahhjaaap/Universal-Desktop-Bridge/releases/download/' + $latest.tag_name
    }
    Get-Download ($base + '/SHA256SUMS') (Join-Path $temp 'SHA256SUMS') 60
    $checksumText = [IO.File]::ReadAllText((Join-Path $temp 'SHA256SUMS'))
    $matchesHash = [regex]::Matches($checksumText,'(?m)^([a-fA-F0-9]{64})\s+udb-windows-x64\.zip\s*$')
    if ($matchesHash.Count -ne 1) { throw 'Release checksum is missing or ambiguous.' }
    $expected = $matchesHash[0].Groups[1].Value.ToLowerInvariant()
    Get-Download ($base + '/udb-windows-x64.zip') $archive 600
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
        [void](Assert-LocalPath $release)
        if ((Get-Sha (Join-Path $release 'udb.exe')) -ne (Get-Sha $candidate) -or (Get-Sha (Join-Path $release 'STANDALONE-SHA256SUMS')) -ne (Get-Sha $manifest)) { throw 'Existing release differs; preserved it.' }
    } else { [IO.Directory]::Move($stage,$release); $stage = $null }
    $exe = Join-Path $release 'udb.exe'
    # Probe only at the final path. Windows can retain an image mapping after
    # a onefile bootloader exits; never execute an image before renaming it.
    $version = (@(& $exe --version) -join '').Trim()
    if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') { throw 'Application verification failed.' }
    $bin = Assert-LocalPath (Join-Path $InstallRoot 'bin')
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
    Write-AtomicText (Join-Path $bin 'udb.ps1') $launcher
    Write-AtomicText (Join-Path $bin 'udb.cmd') "@echo off`r`n`"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe`" -NoLogo -NoProfile -ExecutionPolicy Bypass -File `"%~dp0udb.ps1`" %*`r`nexit /b %errorlevel%`r`n"
    $record = @{product='UniversalDesktopBridge';schema=1;version=$version;exe=$exe;home=$DataHome;archive_sha256=$expected}
    if ($previous) {
        if ($previous.exe -eq $exe) { $record = $previous }
        else { $record.previous = @{product=$previous.product;schema=$previous.schema;version=$previous.version;exe=$previous.exe;home=$previous.home;archive_sha256=$previous.archive_sha256} }
    }
    Set-ActiveRelease $record $previous
    if (!$NoPath) {
        $oldPath = [Environment]::GetEnvironmentVariable('Path','User')
        $parts = @($oldPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $bin.TrimEnd('\') })
        [Environment]::SetEnvironmentVariable('Path',(@($bin) + $parts -join ';'),'User')
        $env:Path = $bin + ';' + $env:Path
    }
    Write-Host ('UDB ' + $version + ' installed. Command: udb') -ForegroundColor Green
    Write-Host ('Local MCP configuration: ' + (Join-Path $InstallRoot 'mcp.json'))
    Write-Host 'Next: udb start | Diagnostics: udb doctor | Optional logon startup: udb autostart enable'
    if (!$NoStart -and !$previous) {
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
