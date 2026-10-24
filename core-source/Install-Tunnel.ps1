[CmdletBinding()]
param([string]$Tag = '')
# Official OpenAI tunnel client only. Verify both published release assets by SHA256.
$ErrorActionPreference = 'Stop'
$Tools = Join-Path $PSScriptRoot 'tools'
New-Item -ItemType Directory -Force -Path $Tools | Out-Null

if ($Tag) {
    if ($Tag -notmatch '^v[0-9]+[.][0-9]+[.][0-9]+$') { throw 'Invalid tunnel-client release tag' }
    $Release = Invoke-RestMethod ("https://api.github.com/repos/openai/tunnel-client/releases/tags/" + $Tag)
} else {
    $Release = Invoke-RestMethod 'https://api.github.com/repos/openai/tunnel-client/releases/latest'
}
if ($Release.tag_name -notmatch '^v[0-9]+[.][0-9]+[.][0-9]+$' -or ($Tag -and $Release.tag_name -ne $Tag)) {
    throw 'Unexpected official tunnel-client release tag'
}
$ZipName = "tunnel-client-$($Release.tag_name)-windows-amd64.zip"
$LicenseName = "tunnel-client-$($Release.tag_name)-windows-amd64-licenses.txt"
$Asset = @($Release.assets | Where-Object { $_.name -eq $ZipName })
$LicenseAsset = @($Release.assets | Where-Object { $_.name -eq $LicenseName })
if ($Asset.Count -ne 1 -or $Asset[0].digest -notmatch '^sha256:[a-f0-9]{64}$') {
    throw 'Cannot verify official Windows tunnel-client asset'
}
if ($LicenseAsset.Count -ne 1 -or $LicenseAsset[0].digest -notmatch '^sha256:[a-f0-9]{64}$') {
    throw 'Cannot verify official tunnel-client license bundle'
}

$Zip = Join-Path $Tools $ZipName
$LicensePath = Join-Path $Tools 'tunnel-LICENSES.txt'
$Client = Join-Path $Tools 'tunnel-client.exe'
$ManifestPath = Join-Path $Tools 'tunnel-client-source.json'
Invoke-WebRequest -UseBasicParsing $Asset[0].browser_download_url -OutFile $Zip
$ZipHash = (Get-FileHash -LiteralPath $Zip -Algorithm SHA256).Hash.ToLowerInvariant()
if ("sha256:$ZipHash" -ne $Asset[0].digest.ToLowerInvariant()) { throw 'Tunnel-client archive digest mismatch' }

Invoke-WebRequest -UseBasicParsing $LicenseAsset[0].browser_download_url -OutFile $LicensePath
$LicenseHash = (Get-FileHash -LiteralPath $LicensePath -Algorithm SHA256).Hash.ToLowerInvariant()
if ("sha256:$LicenseHash" -ne $LicenseAsset[0].digest.ToLowerInvariant()) { throw 'Tunnel-client license digest mismatch' }

$Extract = Join-Path $Tools ("tunnel-unpack-" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Extract | Out-Null
try {
    Expand-Archive -LiteralPath $Zip -DestinationPath $Extract
    $Candidate = Join-Path $Extract 'tunnel-client.exe'
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        $Found = @(Get-ChildItem -LiteralPath $Extract -Filter 'tunnel-client.exe' -File -Recurse)
        if ($Found.Count -ne 1) { throw 'Official archive does not contain exactly one tunnel-client.exe' }
        $Candidate = $Found[0].FullName
    }
    Copy-Item -LiteralPath $Candidate -Destination $Client -Force
} finally {
    if (Test-Path -LiteralPath $Extract) { Remove-Item -LiteralPath $Extract -Recurse -Force }
}

$ExecutableHash = (Get-FileHash -LiteralPath $Client -Algorithm SHA256).Hash.ToLowerInvariant()
& $Client help quickstart
if ($LASTEXITCODE -ne 0) { throw 'Official tunnel-client did not start' }

$Source = [ordered]@{
    repository = 'openai/tunnel-client'
    tag_name = $Release.tag_name
    asset_name = $ZipName
    asset_sha256 = $Asset[0].digest.ToLowerInvariant()
    license_asset_name = $LicenseName
    license_asset_sha256 = $LicenseAsset[0].digest.ToLowerInvariant()
    tunnel_executable_sha256 = $ExecutableHash
    license_file_sha256 = $LicenseHash
}
$TemporaryManifest = $ManifestPath + '.tmp'
$Source | ConvertTo-Json | Set-Content -LiteralPath $TemporaryManifest -Encoding UTF8
Move-Item -LiteralPath $TemporaryManifest -Destination $ManifestPath -Force
Write-Host ("VERIFIED_TUNNEL_CLIENT " + $Release.tag_name)
Write-Host 'Configure tunnel ID and runtime key locally in UniversalDesktopBridge.exe.'
