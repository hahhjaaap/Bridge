# UDB Chrome Aura is opt-in. Run after loading the unpacked extension in Chrome.
[CmdletBinding()]
param([switch]$Disable)
$ErrorActionPreference = 'Stop'
$expected = 'ancaoelohcjcbbmncfhimpookhdelhle'
$folder = Join-Path $env:LOCALAPPDATA 'UniversalDesktopBridge'
$marker = Join-Path $folder 'chrome-aura-enabled'
if ($Disable) {
    if (Test-Path -LiteralPath $marker) {
        Remove-Item -LiteralPath $marker -Force
    }
    Write-Host 'UDB Aura disabled. Normal Chrome tab opening remains available.'
    exit 0
}
Write-Host '1. Open chrome://extensions in your existing Chrome window.'
Write-Host '2. Enable Developer mode and Load unpacked:'
Write-Host ('   ' + (Join-Path (Split-Path $PSScriptRoot -Parent) 'chrome-extension\udb-aura'))
Write-Host '3. Confirm the extension ID displayed in Chrome.'
$id = Read-Host 'Paste the extension ID shown in Chrome'
if ($id -ne $expected) {
    throw 'Extension ID does not match this UDB Aura build; no opt-in marker was written.'
}
New-Item -ItemType Directory -Path $folder -Force | Out-Null
[IO.File]::WriteAllText($marker, $expected + [Environment]::NewLine, [Text.Encoding]::ASCII)
Write-Host 'UDB Aura enabled for the next UDB Chrome-tab request.'
Write-Host 'Restart the installed UDB executable after deploying the new build.'
