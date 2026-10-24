$ErrorActionPreference='Stop'
$cfg=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'installation.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if($cfg.active -notmatch '^[0-9A-Za-z.-]{1,90}$'){throw 'Invalid active release'}
$exe=Join-Path $PSScriptRoot ('releases\'+$cfg.active+'\UniversalDesktopBridge.exe')
if((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash.ToLowerInvariant() -ne $cfg.sha256){throw 'Installed EXE integrity check failed'}
Start-Process -FilePath $exe -ArgumentList @('--home',('"'+$PSScriptRoot+'"')) -WorkingDirectory $PSScriptRoot
