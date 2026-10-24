# Official OpenAI client only. Latest release asset must match GitHub's SHA256 digest.
$ErrorActionPreference = 'Stop'
$Tools = Join-Path $PSScriptRoot 'tools'
New-Item -ItemType Directory -Force -Path $Tools | Out-Null
$Release = Invoke-RestMethod 'https://api.github.com/repos/openai/tunnel-client/releases/latest'
$Asset = @($Release.assets | Where-Object { $_.name -match '^tunnel-client-v.*windows-amd64.zip$' })
if ($Asset.Count -ne 1 -or $Asset[0].digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Cannot verify official Windows asset' }
$Zip = Join-Path $Tools 'tunnel-client.zip'
Invoke-WebRequest -UseBasicParsing $Asset[0].browser_download_url -OutFile $Zip
$Hash = (Get-FileHash $Zip -Algorithm SHA256).Hash.ToLower()
if ("sha256:$Hash" -ne $Asset[0].digest) { throw 'Tunnel client digest mismatch' }
Expand-Archive $Zip -DestinationPath $Tools -Force
$Client = Join-Path $Tools 'tunnel-client.exe'
if (-not (Test-Path $Client)) { throw 'Inspect official archive layout; expected tunnel-client.exe at root' }
& $Client help quickstart
if ($LASTEXITCODE -ne 0) { throw 'Official tunnel-client did not start' }
Write-Host 'Configure tunnel ID and runtime key locally in UniversalDesktopBridge.exe.'
