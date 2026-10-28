$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
try {
    $request = [Console]::In.ReadToEnd() | ConvertFrom-Json
    if ($request.action -eq 'apps') {
        $apps = @(Get-StartApps | Sort-Object Name | Select-Object -First 300 | ForEach-Object {
            @{name = $_.Name; app_id = $_.AppID}
        })
        @{ok = $true; apps = $apps} | ConvertTo-Json -Depth 4 -Compress
    } elseif ($request.action -eq 'notify') {
        # Register only UDB's own notification identity for this Windows user.
        $appId = 'UniversalDesktopBridge'
        $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Software\Classes\AppUserModelId\' + $appId)
        try { $key.SetValue('DisplayName', 'Universal Desktop Bridge') } finally { $key.Dispose() }
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
        $nodes = $xml.GetElementsByTagName('text')
        $nodes.Item(0).AppendChild($xml.CreateTextNode([string]$request.title)) > $null
        $nodes.Item(1).AppendChild($xml.CreateTextNode([string]$request.text)) > $null
        $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
        @{ok = $true; requested = $true; displayed_verified = $false; app_id = $appId} | ConvertTo-Json -Compress
    } else { throw 'Unsupported utility' }
} catch {
    @{ok = $false} | ConvertTo-Json -Compress
    exit 1
}
