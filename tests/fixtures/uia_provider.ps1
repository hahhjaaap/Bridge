$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms -TypeDefinition @'
using System.Windows.Forms;
public class UDBDragPanel : Panel {
 protected override void WndProc(ref Message m) {
  if(m.Msg==0x020E) System.Console.WriteLine("{\"event\":\"horizontal_wheel\"}");
  if(m.Msg==0x0203) System.Console.WriteLine("{\"event\":\"native_double_click\"}");
  base.WndProc(ref m);
 }
}
public class UDBUatForm : Form {
 protected override bool ShowWithoutActivation { get { return true; } }
 protected override CreateParams CreateParams {
  get { var p=base.CreateParams; p.ExStyle |= 0x08000000; return p; }
 }
}
'@
$form=[UDBUatForm]::new()
$form.Text="UDB - Windows Forms test - $PID"
$form.Width=650;$form.Height=340;$form.StartPosition='Manual';$form.Location=[Drawing.Point]::new(1150,620)
$fields=@{}
foreach($entry in @(@('OrderName','Tên đơn thử',20),@('Quantity','Số lượng',65),@('UnitPrice','Đơn giá VND',110))){
 $label=[Windows.Forms.Label]::new();$label.Text=$entry[1];$label.Location=[Drawing.Point]::new(20,$entry[2]);$label.Width=120;$form.Controls.Add($label)
 $box=[Windows.Forms.TextBox]::new();$box.Name=$entry[0];$box.AccessibleName=$entry[0];$box.Location=[Drawing.Point]::new(150,$entry[2]);$box.Width=440;$form.Controls.Add($box);$fields[$entry[0]]=$box
}
$output=[Windows.Forms.Label]::new();$output.Name='Result';$output.AccessibleName='Result';$output.Text='Chưa tính';$output.Location=[Drawing.Point]::new(20,220);$output.Width=590;$output.Height=50;$form.Controls.Add($output)
$calc=[Windows.Forms.Button]::new();$calc.Name='Calculate';$calc.Text='Tính tổng';$calc.Location=[Drawing.Point]::new(20,165);$calc.Width=170;$form.Controls.Add($calc)
$reset=[Windows.Forms.Button]::new();$reset.Name='Reset';$reset.Text='Xóa dữ liệu thử';$reset.Location=[Drawing.Point]::new(210,165);$reset.Width=180;$form.Controls.Add($reset)
$close=[Windows.Forms.Button]::new();$close.Name='CloseTest';$close.Text='Đóng cửa sổ thử';$close.Location=[Drawing.Point]::new(410,165);$close.Width=180;$form.Controls.Add($close)
$calc.Add_Click({
 try {$total=([long]$fields.Quantity.Text)*([long]$fields.UnitPrice.Text);$output.Text="$($fields.OrderName.Text) | Tổng tiền: $total VND"} catch {$output.Text='Lỗi: nhập số nguyên'}
 [Console]::WriteLine((@{event='calculate';output=$output.Text}|ConvertTo-Json -Compress))
})
$reset.Add_Click({foreach($box in $fields.Values){$box.Text=''};$output.Text='Đã xóa dữ liệu thử';[Console]::WriteLine('{"event":"reset"}')})
$close.Add_Click({$form.Close()})
$panel=[UDBDragPanel]::new();$panel.Name='DragPad';$panel.AccessibleName='DragPad';$panel.Location=[Drawing.Point]::new(20,280);$panel.Size=[Drawing.Size]::new(590,100);$panel.BackColor=[Drawing.Color]::LightBlue;$form.Controls.Add($panel)
$panel.Add_MouseDown({param($sender,$e);[Console]::WriteLine((@{event='mouse_down';button=$e.Button.ToString();x=$e.X;y=$e.Y}|ConvertTo-Json -Compress))})
$panel.Add_MouseUp({param($sender,$e);[Console]::WriteLine((@{event='mouse_up';button=$e.Button.ToString();x=$e.X;y=$e.Y}|ConvertTo-Json -Compress))})
$form.Add_Shown({[Console]::WriteLine((@{event='ready';title=$form.Text}|ConvertTo-Json -Compress))})
$form.Add_FormClosed({[Console]::WriteLine('{"event":"closed"}')})
$timer=[Windows.Forms.Timer]::new();$timer.Interval=600000;$timer.Add_Tick({$form.Close()});$timer.Start()
[Windows.Forms.Application]::Run($form)
$timer.Dispose();$form.Dispose()
