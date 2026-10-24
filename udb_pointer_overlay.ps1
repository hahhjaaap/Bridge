$ErrorActionPreference='Stop'
Add-Type -WarningAction SilentlyContinue -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Windows.Forms;
using System.Collections.Concurrent;
using System.Threading;
using System.Runtime.InteropServices;
public class UdbPointer : Form {
 [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
 public static void ConfigureDpi(){SetThreadDpiAwarenessContext(new IntPtr(-4));}
 ConcurrentQueue<string> queue=new ConcurrentQueue<string>();
 System.Windows.Forms.Timer timer=new System.Windows.Forms.Timer(); DateTime deadline=DateTime.UtcNow;
 protected override bool ShowWithoutActivation { get { return true; } }
 protected override CreateParams CreateParams { get { var p=base.CreateParams; p.ExStyle|=0x08000000|0x80|0x20|0x80000; return p; } }
 protected override void WndProc(ref Message m) { if(m.Msg==0x84) { m.Result=(IntPtr)(-1); return; } if(m.Msg==0x21) {m.Result=(IntPtr)3;return;} base.WndProc(ref m); }
 protected override void OnPaint(PaintEventArgs e) {base.OnPaint(e); e.Graphics.FillPolygon(Brushes.DeepSkyBlue,new Point[]{new Point(1,1),new Point(1,25),new Point(7,19),new Point(13,30),new Point(18,27),new Point(12,16),new Point(24,16)});e.Graphics.DrawString("AI",SystemFonts.DefaultFont,Brushes.Navy,24,3);}
 public UdbPointer() { FormBorderStyle=FormBorderStyle.None;ShowInTaskbar=false;TopMost=true;BackColor=Color.Lime;TransparencyKey=Color.Lime;Size=new Size(48,36);StartPosition=FormStartPosition.Manual;Location=new Point(-100,-100);
 var thread=new Thread(()=>{try {string line;while((line=Console.ReadLine())!=null)queue.Enqueue(line);}finally{queue.Enqueue("stop");}});thread.IsBackground=true;thread.Start();
 timer.Interval=30;timer.Tick+=(s,e)=>{string line;while(queue.TryDequeue(out line)){if(line=="stop"){Close();return;}if(line=="hide"){Hide();continue;}var v=line.Split(',');int x,y;if(v.Length==2&&int.TryParse(v[0],out x)&&int.TryParse(v[1],out y)){Location=new Point(x,y);deadline=DateTime.UtcNow.AddSeconds(10);Show();}}if(DateTime.UtcNow>deadline)Hide();};timer.Start();Console.WriteLine("ready");Console.Out.Flush(); }
 protected override void Dispose(bool disposing){if(disposing)timer.Dispose();base.Dispose(disposing);}
}
'@
[UdbPointer]::ConfigureDpi()
[System.Windows.Forms.Application]::Run((New-Object UdbPointer))
