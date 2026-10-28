# Independent visual AI cursor; no OS mouse input and no focus takeover.
$ErrorActionPreference='Stop'
Add-Type -WarningAction SilentlyContinue -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Windows.Forms;
using System.Collections.Concurrent;
using System.Threading;
using System.Runtime.InteropServices;

public sealed class UdbPointer : Form {
  [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
  public static void ConfigureDpi() { SetThreadDpiAwarenessContext(new IntPtr(-4)); }
  readonly ConcurrentQueue<string> queue = new ConcurrentQueue<string>();
  readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
  readonly Color[] palette = new Color[] {
    Color.FromArgb(22,232,255), Color.FromArgb(101,117,255),
    Color.FromArgb(221,82,255), Color.FromArgb(255,96,157),
    Color.FromArgb(24,237,196)
  };
  readonly Font title = new Font("Segoe UI", 10.3f, FontStyle.Bold);
  readonly Font caption = new Font("Segoe UI", 6.8f, FontStyle.Bold);
  readonly DateTime started = DateTime.UtcNow;
  bool stopped = false;
  protected override bool ShowWithoutActivation { get { return true; } }
  protected override CreateParams CreateParams {
    get {
      var p = base.CreateParams;
      p.ExStyle |= 0x08000000 | 0x80 | 0x20 | 0x80000; // no activate, tool, click-through, layered
      return p;
    }
  }
  protected override void WndProc(ref Message m) {
    if (m.Msg == 0x84) { m.Result = (IntPtr)(-1); return; }
    if (m.Msg == 0x21) { m.Result = (IntPtr)3; return; }
    base.WndProc(ref m);
  }
  static GraphicsPath Round(Rectangle r, int radius) {
    GraphicsPath p = new GraphicsPath();
    int d = radius * 2;
    p.AddArc(r.Left, r.Top, d, d, 180, 90);
    p.AddArc(r.Right-d, r.Top, d, d, 270, 90);
    p.AddArc(r.Right-d, r.Bottom-d, d, d, 0, 90);
    p.AddArc(r.Left, r.Bottom-d, d, d, 90, 90);
    p.CloseFigure();
    return p;
  }
  static Color Blend(Color a, Color b, double t) {
    return Color.FromArgb(
      (int)Math.Round(a.R + (b.R-a.R)*t),
      (int)Math.Round(a.G + (b.G-a.G)*t),
      (int)Math.Round(a.B + (b.B-a.B)*t));
  }
  protected override void OnPaint(PaintEventArgs e) {
    base.OnPaint(e);
    Graphics g = e.Graphics;
    g.SmoothingMode = SmoothingMode.AntiAlias;
    g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.ClearTypeGridFit;
    double phase = ((DateTime.UtcNow - started).TotalSeconds / 1.4) % palette.Length;
    int index = (int)Math.Floor(phase);
    Color a = Blend(palette[index], palette[(index+1)%palette.Length], phase-index);
    Color b = palette[(index+2)%palette.Length];
    using (var ring = new Pen(a, 3.0f))
      g.DrawEllipse(ring, 2, 2, 43, 43);
    using (var inner = new Pen(b, 1.4f))
      g.DrawEllipse(inner, 7, 7, 33, 33);
    Point[] arrow = new Point[] {
      new Point(6,5), new Point(6,36), new Point(14,27),
      new Point(21,43), new Point(27,40), new Point(20,25),
      new Point(34,25)
    };
    using (var dark = new SolidBrush(Color.FromArgb(13,21,39)))
      g.FillPolygon(dark, arrow);
    using (var white = new Pen(Color.White, 2.3f))
      g.DrawPolygon(white, arrow);
    Rectangle panel = new Rectangle(48,9,120,44);
    using (GraphicsPath shape = Round(panel, 13)) {
      using (var shadow = new Pen(a, 4.0f))
        g.DrawPath(shadow, shape);
      using (var fill = new SolidBrush(Color.FromArgb(13,20,37)))
        g.FillPath(fill, shape);
      using (var stroke = new LinearGradientBrush(panel,a,b,0f))
        g.DrawPath(new Pen(stroke, 1.7f), shape);
    }
    using (var label = new SolidBrush(Color.FromArgb(248,252,255)))
      g.DrawString("UDB  •  AI", title, label, new PointF(59,13));
    using (var status = new SolidBrush(Color.FromArgb(107,229,248)))
      g.DrawString("CONNECTED", caption, status, new PointF(60,32));
  }
  public UdbPointer() {
    FormBorderStyle=FormBorderStyle.None;
    ShowInTaskbar=false;
    TopMost=true;
    BackColor=Color.Magenta;
    TransparencyKey=Color.Magenta;
    ClientSize=new Size(176,66);
    StartPosition=FormStartPosition.Manual;
    Location=new Point(-500,-500);
    DoubleBuffered=true;
    var reader = new Thread(() => {
      try {
        string line;
        while ((line=Console.ReadLine())!=null) queue.Enqueue(line);
      } finally { queue.Enqueue("stop"); }
    });
    reader.IsBackground=true;
    reader.Start();
    timer.Interval=60;
    timer.Tick += (sender, ev) => {
      string line;
      while (queue.TryDequeue(out line)) {
        if (line=="stop") { stopped=true; Close(); return; }
        if (line=="hide") { Hide(); continue; }
        string[] parts=line.Split(',');
        int x,y;
        if (parts.Length==2 && int.TryParse(parts[0],out x)
            && int.TryParse(parts[1],out y)) {
          Location=new Point(x,y);
          if (!Visible) Show();
        }
      }
      if (Visible && !stopped) Invalidate();
    };
    timer.Start();
    Console.WriteLine("ready");
    Console.Out.Flush();
  }
  protected override void Dispose(bool disposing) {
    if (disposing) { timer.Dispose(); title.Dispose(); caption.Dispose(); }
    base.Dispose(disposing);
  }
}
'@
[UdbPointer]::ConfigureDpi()
[System.Windows.Forms.Application]::Run((New-Object UdbPointer))
