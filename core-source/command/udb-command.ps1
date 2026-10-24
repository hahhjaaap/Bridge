# Universal Desktop Bridge one-command companion v4.1. No credentials stored here.
$ErrorActionPreference = 'Stop'
try {
    $configPath = Join-Path $PSScriptRoot 'udb-command.json'
    $config = [IO.File]::ReadAllText($configPath) | ConvertFrom-Json
    $bridgeDir = [IO.Path]::GetFullPath([string]$config.bridge_home)
    $runtimeDir = $bridgeDir
    $metadataPath = Join-Path $bridgeDir 'installation.json'
    if (Test-Path -LiteralPath $metadataPath) {
        $meta = [IO.File]::ReadAllText($metadataPath) | ConvertFrom-Json
        if ($meta.active -notmatch '^[0-9A-Za-z.-]{1,90}$' -or $meta.active -in @('.', '..')) {
            throw 'Invalid installed release metadata.'
        }
        $runtimeDir = Join-Path $bridgeDir ('releases\' + $meta.active)
    }
    $pythonExe = Join-Path $runtimeDir 'vendor\python-runtime\python.exe'
    $entry = Join-Path $bridgeDir 'UDB-Command\udb_cli.py'
    if (!(Test-Path -LiteralPath $pythonExe -PathType Leaf) -or
        !(Test-Path -LiteralPath $entry -PathType Leaf)) {
        throw 'UDB companion or bundled Python runtime is missing. Reinstall the companion for this installation.'
    }
    # Argument arrays preserve Unicode/spaces; no shell evaluation or system Python.
    & $pythonExe -I $entry --home $bridgeDir @args
    exit $LASTEXITCODE
} catch {
    [Console]::Error.WriteLine('UDB launcher: configuration/runtime unavailable. Run Install-UDB-OneCommand.ps1 again for this installation.')
    exit 1
}
