# Compact independent AI cursor. Native WPF overlay; no Chrome extension, DOM access,
# physical mouse injection or window focus takeover. Screen positions are physical pixels.
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName PresentationFramework,PresentationCore,WindowsBase
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
  [DllImport("user32.dll")] [return: MarshalAs(UnmanagedType.Bool)]
  public static extern bool SetWindowPos(IntPtr hwnd, IntPtr insertAfter,
      int x, int y, int cx, int cy, uint flags);
  public static void StartReader() {
    var reader = new Thread(() => {
      try {
        string line;
        while ((line = Console.ReadLine()) != null) Lines.Enqueue(line);
      } finally { Lines.Enqueue("stop"); }
    });
    reader.IsBackground = true;
    reader.Start();
  }
}
'@
# Windows 10+ per-monitor-v2 DPI context. UDB sends physical screen pixels.
[void][UdbCursorNative]::SetThreadDpiAwarenessContext([IntPtr](-4))
$xaml = @'
<Canvas xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        Width="72" Height="56" Background="Transparent" SnapsToDevicePixels="False">
  <Canvas.Resources>
    <LinearGradientBrush x:Key="accent" StartPoint="0,0" EndPoint="1,1">
      <GradientStop Color="#23E6FF" Offset="0"/>
      <GradientStop Color="#697AFF" Offset=".52"/>
      <GradientStop Color="#DE6EFF" Offset="1"/>
    </LinearGradientBrush>
  </Canvas.Resources>
  <!-- Soft aura is separate from the precise pointer silhouette. -->
  <Ellipse Canvas.Left="1" Canvas.Top="2" Width="39" Height="39"
           Stroke="{StaticResource accent}" StrokeThickness="1.3" Opacity=".52">
    <Ellipse.Effect>
      <DropShadowEffect Color="#568BFF" BlurRadius="10" ShadowDepth="0" Opacity=".65"/>
    </Ellipse.Effect>
  </Ellipse>
  <!-- Dark outer stroke creates contrast on light and dark websites. -->
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
        StrokeLineJoin="Round" StrokeStartLineCap="Round" StrokeEndLineCap="Round"
        Opacity=".95"/>
  <!-- Compact identity chip, not the previous 120px CONNECTED banner. -->
  <Border Canvas.Left="35" Canvas.Top="12" Width="29" Height="22"
          CornerRadius="7" Background="#10192C"
          BorderBrush="{StaticResource accent}" BorderThickness="1.4">
    <Border.Effect>
      <DropShadowEffect Color="#697AFF" BlurRadius="7" ShadowDepth="0" Opacity=".65"/>
    </Border.Effect>
    <TextBlock Text="AI" FontFamily="Segoe UI" FontSize="11" FontWeight="Bold"
               Foreground="#F5FAFF" TextAlignment="Center" VerticalAlignment="Center"/>
  </Border>
  <Ellipse Canvas.Left="62" Canvas.Top="10" Width="4" Height="4"
           Fill="#2EF3DE"/>
</Canvas>
'@
$reader = New-Object System.Xml.XmlNodeReader ([xml]$xaml)
$visual = [System.Windows.Markup.XamlReader]::Load($reader)
$window = New-Object System.Windows.Window
$window.WindowStyle = [System.Windows.WindowStyle]::None
$window.ResizeMode = [System.Windows.ResizeMode]::NoResize
$window.AllowsTransparency = $true
$window.Background = [System.Windows.Media.Brushes]::Transparent
$window.ShowInTaskbar = $false
$window.ShowActivated = $false
$window.Focusable = $false
$window.Topmost = $true
$window.Width = 72
$window.Height = 56
$window.Left = -500
$window.Top = -500
$window.Content = $visual
$script:hwnd = [IntPtr]::Zero
$window.Add_SourceInitialized({
  $helper = New-Object System.Windows.Interop.WindowInteropHelper ($window)
  $script:hwnd = $helper.Handle
  $style = [UdbCursorNative]::GetWindowLong($script:hwnd, -20)
  # WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED
  [void][UdbCursorNative]::SetWindowLong($script:hwnd, -20,
      ($style -bor 0x20 -bor 0x80 -bor 0x08000000 -bor 0x80000))
})
$timer = New-Object System.Windows.Threading.DispatcherTimer
$timer.Interval = [TimeSpan]::FromMilliseconds(35)
$timer.Add_Tick({
  $line = $null
  while ([UdbCursorNative]::Lines.TryDequeue([ref]$line)) {
    if ($line -eq 'stop') { $timer.Stop(); $window.Close(); return }
    if ($line -eq 'hide') { $window.Hide(); continue }
    $parts = $line.Split(',')
    $px = 0
    $py = 0
    if ($parts.Length -eq 2 -and
        [int]::TryParse($parts[0], [ref]$px) -and
        [int]::TryParse($parts[1], [ref]$py)) {
      if (-not $window.IsVisible) { $window.Show() }
      if ($script:hwnd -ne [IntPtr]::Zero) {
        # SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW
        [void][UdbCursorNative]::SetWindowPos(
          $script:hwnd, [IntPtr](-1), ($px - 7), ($py - 5), 0, 0, 0x0051)
      }
    }
  }
})
[UdbCursorNative]::StartReader()
$timer.Start()
[Console]::WriteLine('ready')
[Console]::Out.Flush()
$app = New-Object System.Windows.Application
[void]$app.Run($window)