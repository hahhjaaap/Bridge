[CmdletBinding()]
param(
    [string]$InstallDir=(Join-Path $env:LOCALAPPDATA 'UniversalDesktopBridge-Preview'),
    [switch]$NoLaunch,[switch]$NoShortcut,[switch]$AutoStart,[switch]$VerifyOnly
)
$ErrorActionPreference='Stop'
function Assert-NoLinks([string]$Path) {
    $item=[IO.DirectoryInfo]::new([IO.Path]::GetFullPath($Path))
    while($null -ne $item) {
        if($item.Exists -and ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)){throw 'Linked installation path refused'}
        $item=$item.Parent
    }
}
$manifestPath=Join-Path $PSScriptRoot 'RELEASE-MANIFEST.json'
if(-not (Test-Path -LiteralPath $manifestPath)){throw 'Run Install.ps1 from the extracted release ZIP'}
$manifest=Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$files=@($manifest.files.PSObject.Properties)
$runtimeFiles=@($files | Where-Object {$_.Name -eq 'UniversalDesktopBridge.exe' -or $_.Name.StartsWith('vendor/')})
if($runtimeFiles.Count -lt 10){throw 'Offline runtime dependencies missing'}
foreach($file in $runtimeFiles) {
    $relative=$file.Name
    if($relative -match '(^|/)\.\.?(/|$)|:|\\' -or $relative.StartsWith('/')){throw 'Unsafe package entry'}
    $source=Join-Path $PSScriptRoot $relative
    Assert-NoLinks $source
    $item=Get-Item -LiteralPath $source
    if(($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $item.Length -ne $file.Value.size_bytes){throw "Invalid release file: $relative"}
    if((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.Value.sha256){throw "Release checksum mismatch: $relative"}
}
if($VerifyOnly){Write-Output 'RELEASE_INTEGRITY_OK';return}
$InstallDir=[IO.Path]::GetFullPath($InstallDir)
Assert-NoLinks $InstallDir
if(Test-Path -LiteralPath $InstallDir) {
    if(-not (Test-Path -LiteralPath (Join-Path $InstallDir 'installation.json')) -and @(Get-ChildItem -LiteralPath $InstallDir -Force).Count){throw 'Choose an empty directory or an installation created by this installer'}
}
$prefix=$InstallDir.TrimEnd('\')+'\'
$active=@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)})
if($active.Count){throw 'Close this UDB installation and its tasks before installing'}
$exeHash=($runtimeFiles | Where-Object Name -eq 'UniversalDesktopBridge.exe').Value.sha256
$version=[string]$manifest.version
if($version -notmatch '^[0-9A-Za-z.-]{1,60}$'){throw 'Invalid release version'}
$releaseName=$version+'-'+$exeHash.Substring(0,12)
$release=Join-Path $InstallDir ('releases\'+$releaseName)
if(-not (Test-Path -LiteralPath $release)) {
    $staging=$release+'.staging-'+[Guid]::NewGuid().ToString('N')
    New-Item -ItemType Directory -Path $staging -Force | Out-Null
    try {
        foreach($file in $runtimeFiles) {
            $target=Join-Path $staging $file.Name
            New-Item -ItemType Directory -Path (Split-Path $target) -Force | Out-Null
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file.Name) -Destination $target
        }
        Move-Item -LiteralPath $staging -Destination $release
    } catch {
        if(Test-Path -LiteralPath $staging){Remove-Item -LiteralPath $staging -Recurse -Force}
        throw
    }
}
$metadataPath=Join-Path $InstallDir 'installation.json'
$previous=$null
if(Test-Path -LiteralPath $metadataPath){$previous=Get-Content -LiteralPath $metadataPath -Raw -Encoding UTF8 | ConvertFrom-Json}
$metadata=@{version=$version;active=$releaseName;sha256=$exeHash;previous=$null;previous_sha256=$null;runtime_managed=$true}
if($previous) {
    if($previous.active -eq $releaseName){$metadata.previous=$previous.previous;$metadata.previous_sha256=$previous.previous_sha256}
    else {$metadata.previous=$previous.active;$metadata.previous_sha256=$previous.sha256}
}
$state=Join-Path $InstallDir 'state'
New-Item -ItemType Directory -Path $state -Force | Out-Null
$runtimeConfig=Join-Path $state 'runtime.json'
if((Test-Path -LiteralPath $runtimeConfig) -and (-not $previous -or -not $previous.runtime_managed)){$metadata.runtime_managed=$false}
if($metadata.runtime_managed){@{python_runtime=(Join-Path $release 'vendor\python-runtime')} | ConvertTo-Json | Set-Content -LiteralPath $runtimeConfig -Encoding UTF8}
$metadata | ConvertTo-Json | Set-Content -LiteralPath ($metadataPath+'.tmp') -Encoding UTF8
Move-Item -LiteralPath ($metadataPath+'.tmp') -Destination $metadataPath -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Launch-UDB.ps1') -Destination $InstallDir -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'Rollback.ps1') -Destination $InstallDir -Force
if(-not $NoShortcut) {
    $shell=New-Object -ComObject WScript.Shell
    $shortcut=$shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Programs')) 'Universal Desktop Bridge Preview.lnk'))
    $shortcut.TargetPath='powershell.exe'
    $shortcut.Arguments='-NoProfile -ExecutionPolicy Bypass -File "'+(Join-Path $InstallDir 'Launch-UDB.ps1')+'"'
    $shortcut.WorkingDirectory=$InstallDir;$shortcut.Save()
    if($AutoStart) {
        $startup=$shell.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Startup')) 'Universal Desktop Bridge Preview.lnk'))
        $startup.TargetPath=$shortcut.TargetPath;$startup.Arguments=$shortcut.Arguments;$startup.WorkingDirectory=$InstallDir;$startup.Save()
    }
} elseif($AutoStart) {throw 'AutoStart requires shortcut creation'}
Write-Output ('INSTALLED '+$version+' '+$InstallDir)
if(-not $NoLaunch){& (Join-Path $InstallDir 'Launch-UDB.ps1')}
