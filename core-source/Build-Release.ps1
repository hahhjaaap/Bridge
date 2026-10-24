[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$Revision,
    [string]$OutputDir = (Join-Path $PSScriptRoot 'release')
)
$ErrorActionPreference = 'Stop'
# Developer build only; no pip install, elevation, login, or live EXE replacement.
$Python = (Resolve-Path -LiteralPath $Python).Path
if (Test-Path -LiteralPath $OutputDir) { throw 'Use a new release directory for every build.' }
New-Item -ItemType Directory -Path $OutputDir | Out-Null
$OutputDir = (Resolve-Path -LiteralPath $OutputDir).Path
Push-Location -LiteralPath $PSScriptRoot
try {
    & $Python -m ruff format --check udb/manager.py
    if ($LASTEXITCODE -ne 0) { throw 'Manager format check failed' }
    & $Python -m ruff check udb/manager.py udb/release_policy.py tests/test_manager_lifecycle.py
    if ($LASTEXITCODE -ne 0) { throw 'Ruff static checks failed' }
    & $Python -m mypy --config-file pyproject.toml udb/manager.py udb/release_policy.py
    if ($LASTEXITCODE -ne 0) { throw 'Mypy static checks failed' }
    & $Python -m pytest tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Unit tests failed' }
    & $Python -c 'from pathlib import Path; from udb.release import write_build_info; import sys; write_build_info(Path.cwd(), sys.argv[1])' $Revision
    if ($LASTEXITCODE -ne 0) { throw 'Build provenance failed' }
    & $Python -m PyInstaller --noconfirm --clean --distpath (Join-Path $OutputDir 'bin') --workpath (Join-Path $OutputDir 'work') UniversalDesktopBridge.spec
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed' }
    $Exe = Join-Path $OutputDir 'bin\UniversalDesktopBridge.exe'
    & $Exe --version
    if ($LASTEXITCODE -ne 0) { throw 'Frozen version check failed' }
    & $Exe --list-tools | Out-File -LiteralPath (Join-Path $OutputDir 'tool-catalog.json') -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw 'Frozen catalog failed' }
    $Catalog = Get-Content -Raw -LiteralPath (Join-Path $OutputDir 'tool-catalog.json') | ConvertFrom-Json
    if ($Catalog.tool_count -ne 35) { throw 'Unexpected tool catalog; review API change before release' }
    & $Exe --self-test-credentials
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic credential check failed' }
    & $Exe --gui-check --gui-qa-dir (Join-Path $OutputDir 'gui-qa')
    if ($LASTEXITCODE -ne 0) { throw 'Isolated GUI check failed' }
    & $Python tests\smoke_mcp.py --exe $Exe --isolated
    if ($LASTEXITCODE -ne 0) { throw 'Frozen MCP integration failed' }
    & $Python tests\live_host_background.py --exe $Exe | Out-File -LiteralPath (Join-Path $OutputDir 'host-live-acceptance.json') -Encoding utf8
    if ($LASTEXITCODE -ne 0) { throw 'Frozen host acceptance failed; do not install' }
    $Version = (& $Exe --version).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Frozen version check failed' }
    & $Python Make-Package.py --exe $Exe --output (Join-Path $OutputDir "Universal-Desktop-Bridge-$Version-Windows.zip")
    if ($LASTEXITCODE -ne 0) { throw 'Package verification failed' }
} finally { Pop-Location }
