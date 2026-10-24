# Visible AI pointer, rendered entirely inside Windows Sandbox guest.
# Reads only guest cursor state, never sends mouse/keyboard input.
$ErrorActionPreference='Stop'
try {
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;
public class UdbAiCursor : Form {
  public UdbAiCursor() {
    FormBorderStyle=FormBorderStyle.None;
    ShowInTaskbar=false; TopMost=true;
    BackColor=Color.Magenta; TransparencyKey=Color.Magenta;
    Width=94; Height=65;
    StartPosition=FormStartPosition.Manual;
    Location=new Point(-200,-200);
    DoubleBuffered=true;
  }
  protected override bool ShowWithoutActivation { get { return true; } }
  protected override CreateParams CreateParams {
    get {
      CreateParams cp=base.CreateParams;
      cp.ExStyle |= 0x00000020 | 0x08000000 | 0x00000080; // click-through, no activate, tool window
      return cp;
    }
  }
  protected override void OnPaint(PaintEventArgs e) {
    base.OnPaint(e);
    Graphics g=e.Graphics; g.SmoothingMode=SmoothingMode.AntiAlias;
    using (SolidBrush shade=new SolidBrush(Color.FromArgb(95,0,0,0))) {
      g.FillEllipse(shade, 4,4,39,39);
    }
    using (Pen outer=new Pen(Color.White,3)) {g.DrawEllipse(outer,5,5,36,36);}
    using (Pen ring=new Pen(Color.FromArgb(0,204,255),4)) {g.DrawEllipse(ring,7,7,32,32);}
    using (SolidBrush point=new SolidBrush(Color.FromArgb(1,185,255))) {
      g.FillEllipse(point,18,18,9,9);
    }
    using (SolidBrush bg=new SolidBrush(Color.FromArgb(10,70,105))) {
      g.FillRectangle(bg,40,18,43,25);
    }
    using (Font label=new Font("Segoe UI",11,FontStyle.Bold))
    using (SolidBrush text=new SolidBrush(Color.White)){
      g.DrawString("AI",label,text,45,19);
    }
  }
}
'@
$form=[UdbAiCursor]::new()
$last=''
$timer=[Windows.Forms.Timer]::new()
$timer.Interval=120
$timer.add_Tick({
  try {
    $path='C:\UDBDesktopOutput\cursor.json'
    if(-not [IO.File]::Exists($path)){return}
    $json=[IO.File]::ReadAllText($path)
    if($json -eq $last){return}
    $cursor=$json|ConvertFrom-Json
    if($null -eq $cursor.x -or $null -eq $cursor.y){return}
    $form.Location=[Drawing.Point]::new(([int]$cursor.x-20),([int]$cursor.y-20))
    $form.Invalidate()
    $last=$json
  } catch {
    # Partially written guest state is ignored until next timer tick.
  }
})
$timer.Start()
[Windows.Forms.Application]::Run($form)
} catch {
  [IO.File]::WriteAllText('C:\UDBDesktopOutput\overlay-error.txt',[string]$_.Exception,[Text.Encoding]::UTF8)
}
