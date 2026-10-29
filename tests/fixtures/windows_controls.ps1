param([Parameter(Mandatory=$true)][string]$ReadyPath)
$ErrorActionPreference='Stop'
trap {
 [IO.File]::WriteAllText($ReadyPath+'.error',[string]$_.Exception.Message,[Text.UTF8Encoding]::new($false))
 exit 1
}
Add-Type -AssemblyName PresentationFramework
Add-Type -AssemblyName PresentationCore
# XML entities preserve Unicode even under Windows PowerShell 5.1's ANSI parser.
[xml]$xaml=@'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
 Title="UDB owned UIA fixture" Width="480" Height="540" Left="50" Top="50" ShowActivated="False" ShowInTaskbar="False">
 <StackPanel Margin="20">
  <TextBox x:Name="Editor" AutomationProperties.AutomationId="Editor" Text="&#x1F600; UDB Unicode ki&#x1EC3;m th&#x1EED;"/>
  <TextBox x:Name="Locked" AutomationProperties.AutomationId="Locked" Text="readonly fixture" IsReadOnly="True"/>
  <PasswordBox x:Name="Secret" AutomationProperties.AutomationId="Secret" Password="SYNTHETIC_PASSWORD_DO_NOT_READ"/>
  <CheckBox x:Name="Choice" AutomationProperties.AutomationId="Choice" Content="Fixture choice"/>
  <TextBox x:Name="Counter" AutomationProperties.AutomationId="Counter" Text="0" IsReadOnly="True"/>
  <ComboBox x:Name="Country" AutomationProperties.AutomationId="Country" SelectedIndex="0">
   <ComboBoxItem Content="Choose"/><ComboBoxItem Content="Vietnam"/><ComboBoxItem Content="Japan"/>
  </ComboBox>
  <ListBox x:Name="Items" AutomationProperties.AutomationId="Items" Height="85">
   <ListBoxItem AutomationProperties.AutomationId="First" Content="First item"/>
   <ListBoxItem AutomationProperties.AutomationId="Second" Content="Second item"/>
  </ListBox>
  <Expander x:Name="Details" AutomationProperties.AutomationId="Details" Header="Details"><TextBox Text="Expanded content"/></Expander>
  <ListBox x:Name="LongList" AutomationProperties.AutomationId="LongList" Height="55" VirtualizingStackPanel.IsVirtualizing="False"/>
 </StackPanel>
</Window>
'@
$window=[Windows.Markup.XamlReader]::Load([Xml.XmlNodeReader]::new($xaml))
$counter=$window.FindName('Counter')
$choice=$window.FindName('Choice')
$choice.Add_Checked({$counter.Text=([int]$counter.Text+1).ToString()})
$choice.Add_Unchecked({$counter.Text=([int]$counter.Text+1).ToString()})
$long=$window.FindName('LongList')
for($i=0;$i -lt 20;$i++){
 $item=[Windows.Controls.ListBoxItem]::new();$item.Content="Row $i"
 [Windows.Automation.AutomationProperties]::SetAutomationId($item,"Row$i")
 $long.Items.Add($item)>$null
}
$window.Add_ContentRendered({
 $handle=[Windows.Interop.WindowInteropHelper]::new($window).Handle.ToInt64()
 [IO.File]::WriteAllText($ReadyPath+'.tmp',(@{hwnd=$handle;pid=$PID}|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
 [IO.File]::Move($ReadyPath+'.tmp',$ReadyPath)
})
$window.Add_Closed({[Windows.Threading.Dispatcher]::CurrentDispatcher.BeginInvokeShutdown([Windows.Threading.DispatcherPriority]::Normal)})
$window.Show()
[Windows.Threading.Dispatcher]::Run()
