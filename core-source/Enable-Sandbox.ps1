# Optional: run manually in an elevated PowerShell. Never restarts Windows automatically.
# This changes a Windows feature; review before execution.
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run PowerShell as Administrator, then run this optional script.'
}
Enable-WindowsOptionalFeature -Online -FeatureName 'Containers-DisposableClientVM' -All -NoRestart
Write-Host 'Restart Windows manually when convenient. Bridge never falls back to host shell execution.'
