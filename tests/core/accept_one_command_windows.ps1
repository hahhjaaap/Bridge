# Synthetic Windows acceptance; no changes to the real UDB installation or user PATH.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$RuntimePython)
$ErrorActionPreference = 'Stop'
$source = Split-Path $PSScriptRoot -Parent
$fixture = Join-Path ([IO.Path]::GetTempPath()) ('UDB-Cmd-Accept-' + [Guid]::NewGuid().ToString('N'))
$bridge = Join-Path $fixture ('Nguy' + [char]0x1EC5 + 'n Anh & UDB')
$fakeLocalApp = Join-Path $fixture 'LocalAppData'
$commandDir = Join-Path $fakeLocalApp 'Microsoft\WindowsApps'
$oldLocalApp = $env:LOCALAPPDATA
$oldPath = $env:Path
try {
    [IO.Directory]::CreateDirectory((Join-Path $bridge 'vendor\python-runtime')) > $null
    [IO.Directory]::CreateDirectory((Join-Path $bridge 'state')) > $null
    [IO.Directory]::CreateDirectory($commandDir) > $null
    Copy-Item -Path (Join-Path (Split-Path $RuntimePython -Parent) '*') -Destination (Join-Path $bridge 'vendor\python-runtime') -Recurse
    [IO.File]::WriteAllText((Join-Path $bridge 'UniversalDesktopBridge.exe'), 'SYNTHETIC-NOT-EXECUTABLE')
    $prefs = @{'gui.json'='{"autoconnect":false,"motion":false}';'host-settings.json'='{"enabled":false,"owner":"preserve"}'}
    foreach($name in $prefs.Keys) { [IO.File]::WriteAllText((Join-Path $bridge ('state\' + $name)), $prefs[$name]) }
    # Isolate command lookup in THIS child process, including pre-existing udb aliases on PATH.
    $env:LOCALAPPDATA = $fakeLocalApp
    $env:Path = $commandDir + ';' + $env:SystemRoot + '\System32;' + $env:SystemRoot + '\System32\WindowsPowerShell\v1.0'
    foreach ($script in @('Install-UDB-OneCommand.ps1', 'command\udb-command.ps1')) {
        $tokens=$null; $errors=$null
        [Management.Automation.Language.Parser]::ParseFile((Join-Path $source $script), [ref]$tokens, [ref]$errors) > $null
        if ($errors.Count) { throw ('PowerShell parse failed: ' + $script) }
    }
    & (Join-Path $source 'Install-UDB-OneCommand.ps1') -BridgeHome $bridge -NoLaunch
    if (!(Test-Path -LiteralPath (Join-Path $commandDir 'udb.cmd'))) { throw 'Command was not installed.' }
    $output = & (Join-Path $commandDir 'udb.cmd') status --json
    $statusExit = $LASTEXITCODE
    $report = $output | ConvertFrom-Json
    if ($statusExit -ne 2 -or $report.manager_running -ne $false -or $report.background -ne 'disabled') {
        throw 'CMD -> PowerShell -> bundled Python status/exit-code acceptance failed.'
    }
    foreach($name in $prefs.Keys) {
        if ([IO.File]::ReadAllText((Join-Path $bridge ('state\' + $name))) -cne $prefs[$name]) { throw 'Owner settings changed.' }
    }
    & (Join-Path $source 'Install-UDB-OneCommand.ps1') -BridgeHome $bridge -NoLaunch
    [IO.File]::WriteAllText((Join-Path $commandDir 'udb.cmd'), '@echo unrelated-command')
    $refused = $false
    try { & (Join-Path $source 'Install-UDB-OneCommand.ps1') -BridgeHome $bridge -NoLaunch }
    catch { $refused = $true }
    if (!$refused -or [IO.File]::ReadAllText((Join-Path $commandDir 'udb.cmd')) -cne '@echo unrelated-command') {
        throw 'Unrelated command was not preserved.'
    }
    [pscustomobject]@{accepted=$true; unicode_path=$true; cmd_powershell_python=$true;
        exit_codes=$true; idempotent_install=$true; preferences_unchanged=$true; collision_refused=$true;
        powershell=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Compress
} finally {
    $env:LOCALAPPDATA = $oldLocalApp
    $env:Path = $oldPath
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
