# UDB native AI cursor and tab-strip identity. No Chrome extension or CDP.
# Windows UI Automation observes only tab runtime IDs, selection and bounds.
# Other Chrome tabs and other applications never display the AI cursor.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework,PresentationCore,WindowsBase,UIAutomationClient,UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Collections.Concurrent;
using System.Runtime.InteropServices;
using System.Threading;
public static class UdbCursorNative {
  public static readonly ConcurrentQueue<string> Lines = new ConcurrentQueue<string>();
  [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
  [DllImport("user32.dll", EntryPoint="GetWindowLongW")]
  public static extern int GetWindowLong(IntPtr hwnd, int index);
  [DllImport("user32.dll", EntryPoint="SetWindowLongW")]
  public static extern int SetWindowLong(IntPtr hwnd, int index, int value);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(
      IntPtr hwnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(
      IntPtr hwnd, out uint pid);
  public static void StartReader() {
    var thread = new Thread(() => {
      try {
        string line;
        while ((line = Console.ReadLine()) != null) Lines.Enqueue(line);
      } finally { Lines.Enqueue("stop"); }
    });
    thread.IsBackground = true;
    thread.Start();
  }
}
'@
[void][UdbCursorNative]::SetThreadDpiAwarenessContext([IntPtr](-4))
$cursorXaml = @'
<Canvas xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="72" Height="56" Background="Transparent">
  <Canvas.Resources>
    <LinearGradientBrush x:Key="accent" StartPoint="0,0" EndPoint="1,1">
      <GradientStop Color="#23E6FF" Offset="0"/>
      <GradientStop Color="#697AFF" Offset=".52"/>
      <GradientStop Color="#DE6EFF" Offset="1"/>
    </LinearGradientBrush>
  </Canvas.Resources>
  <Ellipse Canvas.Left="1" Canvas.Top="2" Width="39" Height="39"
           Stroke="{StaticResource accent}" StrokeThickness="1.3" Opacity=".52">
    <Ellipse.Effect>
      <DropShadowEffect Color="#568BFF" BlurRadius="10" ShadowDepth="0" Opacity=".65"/>
    </Ellipse.Effect>
  </Ellipse>
  <Path Data="M 7,5 L 7,37 L 14,29 L 21,44 L 27,41 L 20,26 L 34,26 Z"
        Fill="#0B1427" Stroke="#09111F" StrokeThickness="4"
        StrokeLineJoin="Round">
    <Path.Effect>
      <DropShadowEffect Color="#4A66FF" BlurRadius="8" ShadowDepth="0" Opacity=".75"/>
    </Path.Effect>
  </Path>
  <Path Data="M 7,5 L 7,37 L 14,29 L 21,44 L 27,41 L 20,26 L 34,26 Z"
        Fill="#101C32" Stroke="#F7FDFF" StrokeThickness="2.1"
        StrokeLineJoin="Round"/>
  <Path Data="M 10,11 L 10,29 L 14,25 L 21,39"
        Stroke="{StaticResource accent}" StrokeThickness="1.8"
        StrokeLineJoin="Round" StrokeStartLineCap="Round" StrokeEndLineCap="Round"/>
  <Border Canvas.Left="35" Canvas.Top="12" Width="29" Height="22"
          CornerRadius="7" Background="#10192C"
          BorderBrush="{StaticResource accent}" BorderThickness="1.4">
    <TextBlock Text="AI" FontFamily="Segoe UI" FontSize="11" FontWeight="Bold"
               Foreground="#F5FAFF" TextAlignment="Center" VerticalAlignment="Center"/>
  </Border>
  <Ellipse Canvas.Left="62" Canvas.Top="10" Width="4" Height="4" Fill="#2EF3DE"/>
</Canvas>
'@
$badgeXaml = @'
<Canvas xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="27" Height="24" Background="Transparent">
  <Canvas.Resources>
    <LinearGradientBrush x:Key="accent" StartPoint="0,0" EndPoint="1,1">
      <GradientStop Color="#2DE7FF" Offset="0"/>
      <GradientStop Color="#B66CFF" Offset="1"/>
    </LinearGradientBrush>
  </Canvas.Resources>
  <Border Canvas.Left="1" Canvas.Top="1" Width="24" Height="22"
          CornerRadius="7" Background="#151B31"
          BorderBrush="{StaticResource accent}" BorderThickness="1.4">
    <Border.Effect>
      <DropShadowEffect Color="#A052FF" BlurRadius="5" ShadowDepth="0" Opacity=".7"/>
    </Border.Effect>
  </Border>
  <Path Data="M 6,5 L 6,18 L 10,15 L 13,21 L 16,19 L 13,13 L 19,13 Z"
        Fill="#121D30" Stroke="#F6FCFF" StrokeThickness="1.5"
        StrokeLineJoin="Round"/>
  <TextBlock Canvas.Left="15" Canvas.Top="3" Text="AI" FontFamily="Segoe UI"
             FontSize="7" FontWeight="Bold" Foreground="#39E9FF"/>
</Canvas>
'@
function New-Overlay([string]$markup, [int]$width, [int]$height) {
  $xml = New-Object System.Xml.XmlNodeReader ([xml]$markup)
  $visual = [System.Windows.Markup.XamlReader]::Load($xml)
  $w = New-Object System.Windows.Window
  $w.WindowStyle = [System.Windows.WindowStyle]::None
  $w.ResizeMode = [System.Windows.ResizeMode]::NoResize
  $w.AllowsTransparency = $true
  $w.Background = [System.Windows.Media.Brushes]::Transparent
  $w.ShowInTaskbar = $false
  $w.ShowActivated = $false
  $w.Focusable = $false
  $w.Topmost = $true
  $w.Width = $width
  $w.Height = $height
  $w.Left = -500
  $w.Top = -500
  $w.Content = $visual
  $w.Add_SourceInitialized({
    param($sender, $eventArgs)
    $handle = (New-Object System.Windows.Interop.WindowInteropHelper ($sender)).Handle
    $style = [UdbCursorNative]::GetWindowLong($handle, -20)
    # Click-through, toolwindow, no activation, layered.
    [void][UdbCursorNative]::SetWindowLong(
      $handle, -20, ($style -bor 0x20 -bor 0x80 -bor 0x08000000 -bor 0x80000))
  })
  return $w
}
function Place-Overlay($w, [int]$x, [int]$y) {
  if (-not $w.IsVisible) { $w.Show() }
  $handle = (New-Object System.Windows.Interop.WindowInteropHelper ($w)).Handle
  # HWND_TOPMOST, SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW.
  [void][UdbCursorNative]::SetWindowPos($handle, [IntPtr](-1), $x, $y, 0, 0, 0x0051)
}
$script:cursor = New-Overlay $cursorXaml 72 56
$script:badges = @{}
$script:markerFile = Join-Path $env:LOCALAPPDATA 'UniversalDesktopBridge\ai-chrome-tabs.json'
$script:markerStamp = [DateTime]::MinValue
$script:registered = @()
$script:position = $null
$script:active = $false
$script:pageBounds = $null
$script:lastPoll = [DateTime]::MinValue
$script:tabCondition = New-Object System.Windows.Automation.PropertyCondition(
  [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
  [System.Windows.Automation.ControlType]::TabItem
)
function Hide-Cursor {
  if ($script:cursor.IsVisible) { $script:cursor.Hide() }
}
function Hide-Badges {
  foreach ($w in $script:badges.Values) {
    if ($w.IsVisible) { $w.Hide() }
  }
}
function Update-Cursor {
  if (-not $script:active -or $null -eq $script:position -or $null -eq $script:pageBounds) {
    Hide-Cursor
    return
  }
  $x = [int]$script:position[0]
  $y = [int]$script:position[1]
  $b = $script:pageBounds
  if ($x -lt $b[0] -or $x -ge $b[2] -or $y -lt $b[1] -or $y -ge $b[3]) {
    Hide-Cursor
    return
  }
  Place-Overlay $script:cursor ($x - 7) ($y - 5)
}
function Update-TabState {
  $script:active = $false
  $script:pageBounds = $null
  try {
    if (-not [IO.File]::Exists($script:markerFile)) {
      $script:registered = @()
      Hide-Badges
      Hide-Cursor
      return
    }
    $stamp = [IO.File]::GetLastWriteTimeUtc($script:markerFile)
    if ($stamp -ne $script:markerStamp) {
      $script:markerStamp = $stamp
      $file = Get-Item -LiteralPath $script:markerFile
      $script:registered = @()
      if ($file.Length -gt 0 -and $file.Length -le 4096) {
        $data = Get-Content -LiteralPath $script:markerFile -Raw | ConvertFrom-Json
        if ($data.version -eq 1) {
          $script:registered = @($data.tabs | Select-Object -Last 8)
        }
      }
    }
    $foreground = [UdbCursorNative]::GetForegroundWindow()
    if ($foreground -eq [IntPtr]::Zero -or -not [UdbCursorNative]::IsWindow($foreground) -or
        [UdbCursorNative]::IsIconic($foreground)) {
      Hide-Badges
      Hide-Cursor
      return
    }
    $chromePid = [uint32]0
    [void][UdbCursorNative]::GetWindowThreadProcessId($foreground, [ref]$chromePid)
    $matching = @($script:registered | Where-Object {
      $_ -and [long]$_.hwnd -eq $foreground.ToInt64() -and [uint32]$_.pid -eq $chromePid
    })
    if ($matching.Count -eq 0) {
      Hide-Badges
      Hide-Cursor
      return
    }
    $root = [System.Windows.Automation.AutomationElement]::FromHandle($foreground)
    $chromeRect = $root.Current.BoundingRectangle
    $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants, $script:tabCondition)
    $observed = @{}
    $selectedTotal = 0
    for ($i = 0; $i -lt [Math]::Min(100, $items.Count); $i++) {
      try {
        $item = $items.Item($i)
        if ($item.Current.IsOffscreen) { continue }
        $id = $item.GetRuntimeId() -join '.'
        $rect = $item.Current.BoundingRectangle
        if ($rect.Width -le 0 -or $rect.Height -le 0 -or
            $rect.Height -gt 65 -or $rect.Y -lt $chromeRect.Y -or
            $rect.Y -ge ($chromeRect.Y + 65) -or
            $rect.X -lt $chromeRect.X -or $rect.Right -gt ($chromeRect.Right + 2)) {
          continue
        }
        $pattern = $null
        $selected = $false
        if ($item.TryGetCurrentPattern(
            [System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pattern)) {
          $selected = $pattern.Current.IsSelected
        }
        if ($selected) { $selectedTotal++ }
        $observed[$id] = [pscustomobject]@{Rect=$rect;Selected=$selected}
      } catch { }
    }
    $shown = @{}
    foreach ($entry in $matching) {
      $id = [string]$entry.id
      if (-not $observed.ContainsKey($id)) { continue }
      $tab = $observed[$id]
      if (-not $script:badges.ContainsKey($id)) {
        $script:badges[$id] = New-Overlay $badgeXaml 27 24
      }
      $badge = $script:badges[$id]
      Place-Overlay $badge ([int][Math]::Round($tab.Rect.X + 6)) (
        [int][Math]::Round($tab.Rect.Y + 8))
      $shown[$id] = $true
      # Split-view may report two selected tabs. Fail closed rather than
      # drawing an AI cursor over the user's other page.
      if ($tab.Selected -and $selectedTotal -eq 1) {
        $script:active = $true
        $script:pageBounds = @(
          [int][Math]::Round($chromeRect.X + 8),
          [int][Math]::Round($chromeRect.Y + 95),
          [int][Math]::Round($chromeRect.Right - 8),
          [int][Math]::Round($chromeRect.Bottom - 12)
        )
      }
    }
    $registeredIds = @($script:registered | ForEach-Object { [string]$_.id })
    foreach ($id in @($script:badges.Keys)) {
      if ($id -notin $registeredIds) {
        $script:badges[$id].Close()
        $script:badges.Remove($id)
      } elseif (-not $shown.ContainsKey($id)) {
        $script:badges[$id].Hide()
      }
    }
    Update-Cursor
  } catch {
    if ($env:UDB_CURSOR_DEBUG) {
      Add-Content -LiteralPath (Join-Path $env:TEMP 'udb-cursor-debug.log') -Value (
        [DateTime]::UtcNow.ToString('o') + ' ' + $_.Exception.ToString())
    }
    $script:active = $false
    Hide-Badges
    Hide-Cursor
  }
}
$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(40)
$timer.Add_Tick({
  $line = $null
  while ([UdbCursorNative]::Lines.TryDequeue([ref]$line)) {
    if ($line -eq 'stop') {
      $timer.Stop()
      foreach ($badge in $script:badges.Values) { $badge.Close() }
      $script:cursor.Close()
      return
    }
    if ($line -eq 'hide') {
      $script:position = $null
      Hide-Cursor
      continue
    }
    $parts = $line.Split(',')
    $x = 0; $y = 0
    if ($parts.Length -eq 2 -and [int]::TryParse($parts[0], [ref]$x) -and
        [int]::TryParse($parts[1], [ref]$y)) {
      $script:position = @($x,$y)
      Update-Cursor
    }
  }
  if (([DateTime]::UtcNow - $script:lastPoll).TotalMilliseconds -ge 350) {
    $script:lastPoll = [DateTime]::UtcNow
    Update-TabState
  }
})
[UdbCursorNative]::StartReader()
$timer.Start()
[Console]::WriteLine('ready')
[Console]::Out.Flush()
$app = New-Object System.Windows.Application
$app.ShutdownMode = [System.Windows.ShutdownMode]::OnMainWindowClose
[void]$app.Run($script:cursor)
