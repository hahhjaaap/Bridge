$ErrorActionPreference='Stop'
$path=Join-Path $PSScriptRoot 'installation.json'
$cfg=Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
if(-not $cfg.previous -or $cfg.previous -notmatch '^[0-9A-Za-z.-]{1,90}$'){throw 'No previous installed release to restore'}
$prefix=$PSScriptRoot.TrimEnd('\')+'\'
if(@(Get-CimInstance Win32_Process | Where-Object {$_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix,[StringComparison]::OrdinalIgnoreCase)}).Count){throw 'Close this UDB installation and its tasks before rollback'}
$exe=Join-Path $PSScriptRoot ('releases\'+$cfg.previous+'\UniversalDesktopBridge.exe')
if((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $cfg.previous_sha256){throw 'Previous EXE integrity check failed'}
$old=$cfg.active;$oldHash=$cfg.sha256
$cfg.active=$cfg.previous;$cfg.sha256=$cfg.previous_sha256
$cfg.previous=$old;$cfg.previous_sha256=$oldHash
if($cfg.runtime_managed){@{python_runtime=(Join-Path (Split-Path $exe) 'vendor\python-runtime')} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'state\runtime.json') -Encoding UTF8}
$cfg | ConvertTo-Json | Set-Content -LiteralPath ($path+'.tmp') -Encoding UTF8
Move-Item -LiteralPath ($path+'.tmp') -Destination $path -Force
Write-Output ('ROLLED_BACK '+$cfg.active)
