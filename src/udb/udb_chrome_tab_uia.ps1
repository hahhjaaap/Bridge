# Read only the Chrome tab strip via Windows UI Automation; no extension/CDP/DOM.
param([Parameter(Mandatory=$true)][long]$Hwnd)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes
try {
  $root = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]$Hwnd)
  if ($null -eq $root) { throw 'Window unavailable' }
  $condition = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::TabItem
  )
  $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
  $chromeRect = $root.Current.BoundingRectangle
  $rows = @(
    foreach ($item in $items) {
      try {
        $rid = $item.GetRuntimeId()
        $rect = $item.Current.BoundingRectangle
        $pattern = $null
        $selected = $false
        if ($item.TryGetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
          $selected = $pattern.Current.IsSelected
        }
        # Ignore tab controls inside websites; only Chrome's top tab strip.
        if ($rid.Length -gt 0 -and $rect.Width -gt 0 -and $rect.Height -gt 0 -and
            $rect.Height -le 65 -and $rect.Y -ge $chromeRect.Y -and
            $rect.Y -lt ($chromeRect.Y + 65) -and
            $rect.X -ge $chromeRect.X -and $rect.Right -le ($chromeRect.Right + 2)) {
          [pscustomobject]@{
            id = ($rid -join '.')
            selected = [bool]$selected
            bounds = @([int][Math]::Round($rect.X),[int][Math]::Round($rect.Y),
                       [int][Math]::Round($rect.Width),[int][Math]::Round($rect.Height))
          }
        }
      } catch { }
    }
  )
  [pscustomobject]@{ok=$true; tabs=$rows} | ConvertTo-Json -Depth 5 -Compress
} catch {
  [pscustomobject]@{ok=$false; tabs=@()} | ConvertTo-Json -Compress
  exit 1
}
