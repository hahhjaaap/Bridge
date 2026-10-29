# Independent, per-pixel-alpha AI identity. No OS input or focus takeover.
param([string]$PreviewPath = '')
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase,System.Web.Extensions
$references = @('System.Windows.Forms','System.Drawing','System.Web.Extensions',
  [System.Windows.Automation.AutomationElement].Assembly.Location,
  [System.Windows.Automation.ControlType].Assembly.Location,
  [System.Windows.Rect].Assembly.Location)
Add-Type -WarningAction SilentlyContinue -ReferencedAssemblies $references -TypeDefinition @'
using System;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Windows.Forms;
using System.Collections.Concurrent;
using System.Collections.Generic;
using System.IO;
using System.Web.Script.Serialization;
using System.Windows.Automation;
using System.Threading;
using System.Runtime.InteropServices;

public sealed class UdbPointer : Form {
  [StructLayout(LayoutKind.Sequential)] struct PointNative { public int X,Y; public PointNative(int x,int y){X=x;Y=y;} }
  [StructLayout(LayoutKind.Sequential)] struct SizeNative { public int Width,Height; public SizeNative(int w,int h){Width=w;Height=h;} }
  [StructLayout(LayoutKind.Sequential,Pack=1)] struct Blend { public byte Op,Flags,Alpha,Format; }
  [DllImport("user32.dll")] static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
  [DllImport("user32.dll")] static extern IntPtr GetDC(IntPtr hwnd);
  [DllImport("user32.dll")] static extern int ReleaseDC(IntPtr hwnd,IntPtr dc);
  [DllImport("gdi32.dll")] static extern IntPtr CreateCompatibleDC(IntPtr dc);
  [DllImport("gdi32.dll")] static extern IntPtr SelectObject(IntPtr dc,IntPtr obj);
  [DllImport("gdi32.dll")] static extern bool DeleteObject(IntPtr obj);
  [DllImport("gdi32.dll")] static extern bool DeleteDC(IntPtr dc);
  [DllImport("user32.dll",SetLastError=true)] static extern bool UpdateLayeredWindow(IntPtr hwnd,IntPtr dst,ref PointNative pos,ref SizeNative size,IntPtr src,ref PointNative origin,int key,ref Blend blend,int flags);
  [DllImport("user32.dll")] static extern IntPtr MonitorFromPoint(PointNative point,int flags);
  [DllImport("shcore.dll")] static extern int GetDpiForMonitor(IntPtr monitor,int kind,out uint x,out uint y);
  [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] static extern bool IsWindow(IntPtr hwnd);
  [DllImport("user32.dll")] static extern bool IsIconic(IntPtr hwnd);
  [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
  static float ScaleAt(Point target){
    try{uint x,y;if(GetDpiForMonitor(MonitorFromPoint(new PointNative(target.X,target.Y),2),0,out x,out y)==0)return Math.Max(1f,Math.Min(2.5f,x/96f));}
    catch(DllNotFoundException){}catch(EntryPointNotFoundException){}
    return 1f;
  }
  public static void ConfigureDpi(){SetThreadDpiAwarenessContext(new IntPtr(-4));}
  readonly ConcurrentQueue<string> queue = new ConcurrentQueue<string>();
  readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
  readonly Font font = new Font("Segoe UI",8.2f,FontStyle.Bold);
  bool working = false, stopped = false;
  Point target;
  readonly bool badgeOnly;
  bool enabled=false, activeTab=false;
  Rectangle pageBounds;
  DateTime lastPoll;
  readonly Dictionary<string,UdbPointer> badges=new Dictionary<string,UdbPointer>();
  // Observe only tab runtime IDs, selection and geometry, never names/URLs/content.
  bool PollTabs(){
    activeTab=false;
    var shown=new HashSet<string>();
    try{
      string path=Path.Combine(Environment.GetEnvironmentVariable("LOCALAPPDATA"),"UniversalDesktopBridge","ai-chrome-tabs.json");
      var file=new FileInfo(path);
      if(!file.Exists||file.Length==0||file.Length>4096)return false;
      var data=new JavaScriptSerializer{MaxJsonLength=4096}.DeserializeObject(File.ReadAllText(path)) as Dictionary<string,object>;
      if(data==null||!data.ContainsKey("version")||Convert.ToInt32(data["version"])!=1||!data.ContainsKey("tabs"))return false;
      var records=data["tabs"] as object[];
      if(records==null||records.Length>8)return false;
      IntPtr hwnd=GetForegroundWindow();uint pid;
      if(hwnd==IntPtr.Zero||!IsWindow(hwnd)||IsIconic(hwnd))return false;
      GetWindowThreadProcessId(hwnd,out pid);
      var ids=new HashSet<string>();
      foreach(var value in records){
        var row=value as Dictionary<string,object>;
        if(row!=null&&Convert.ToInt64(row["hwnd"])==hwnd.ToInt64()&&Convert.ToUInt32(row["pid"])==pid){
          var id=row["id"] as string;if(!String.IsNullOrEmpty(id)&&id.Length<=150)ids.Add(id);
        }
      }
      if(ids.Count==0)return false;
      var root=AutomationElement.FromHandle(hwnd);var bounds=root.Current.BoundingRectangle;
      var condition=new PropertyCondition(AutomationElement.ControlTypeProperty,ControlType.TabItem);
      var items=root.FindAll(TreeScope.Descendants,condition);
      int selectedTotal=0;bool selectedOwned=false;
      var matches=new Dictionary<string,System.Windows.Rect>();
      for(int i=0;i<Math.Min(100,items.Count);i++){
        var item=items[i];var rect=item.Current.BoundingRectangle;
        if(item.Current.IsOffscreen||rect.Width<=0||rect.Height<=0||rect.Height>65||rect.Y<bounds.Y||rect.Y>=bounds.Y+65||rect.X<bounds.X||rect.Right>bounds.Right+2)continue;
        object pattern;bool selected=item.TryGetCurrentPattern(SelectionItemPattern.Pattern,out pattern)&&((SelectionItemPattern)pattern).Current.IsSelected;
        if(selected)selectedTotal++;
        string id=String.Join(".",item.GetRuntimeId());
        if(ids.Contains(id)){matches[id]=rect;if(selected)selectedOwned=true;}
      }
      foreach(var pair in matches){
        UdbPointer badge;
        if(!badges.TryGetValue(pair.Key,out badge)){badge=new UdbPointer(true);badges.Add(pair.Key,badge);}
        badge.target=new Point((int)Math.Round(pair.Value.X+6),(int)Math.Round(pair.Value.Y+8));
        badge.Show();badge.RenderFrame();shown.Add(pair.Key);
      }
      activeTab=selectedTotal==1&&selectedOwned;
      pageBounds=Rectangle.FromLTRB((int)Math.Ceiling(bounds.Left),(int)Math.Ceiling(bounds.Top+65),(int)Math.Floor(bounds.Right),(int)Math.Floor(bounds.Bottom));
    }catch{activeTab=false;shown.Clear();}
    finally{
      var expired=new List<string>();
      foreach(var pair in badges)if(!shown.Contains(pair.Key)){pair.Value.Dispose();expired.Add(pair.Key);}
      foreach(string id in expired)badges.Remove(id);
    }
    return activeTab;
  }
  bool MayShow(){return enabled&&activeTab&&(!working||pageBounds.Contains(target));}
  DateTime lastAction;
  protected override bool ShowWithoutActivation { get{return true;} }
  protected override CreateParams CreateParams {
    get {var p=base.CreateParams;p.ExStyle|=0x08000000|0x80|0x20|0x80000;return p;}
  }
  protected override void WndProc(ref Message m){
    if(m.Msg==0x84){m.Result=(IntPtr)(-1);return;}
    if(m.Msg==0x21){m.Result=(IntPtr)3;return;}
    base.WndProc(ref m);
  }
  static GraphicsPath Round(RectangleF r,float radius){
    var p=new GraphicsPath();float d=radius*2;
    p.AddArc(r.Left,r.Top,d,d,180,90);p.AddArc(r.Right-d,r.Top,d,d,270,90);
    p.AddArc(r.Right-d,r.Bottom-d,d,d,0,90);p.AddArc(r.Left,r.Bottom-d,d,d,90,90);
    p.CloseFigure();return p;
  }
  static void DrawPill(Graphics g,RectangleF box,string text,Font font){
    using(var shape=Round(box,box.Height/2)){
      using(var fill=new SolidBrush(Color.FromArgb(245,21,30,36)))g.FillPath(fill,shape);
      using(var stroke=new Pen(Color.FromArgb(56,92,80),1))g.DrawPath(stroke,shape);
    }
    using(var dot=new SolidBrush(Color.FromArgb(120,223,188)))g.FillEllipse(dot,box.X+10,box.Y+box.Height/2-2,4,4);
    using(var fg=new SolidBrush(Color.FromArgb(232,246,240)))
    using(var format=new StringFormat()){format.LineAlignment=StringAlignment.Center;g.DrawString(text,font,fg,new RectangleF(box.X+21,box.Y,box.Width-24,box.Height),format);}
  }
  static void DrawScene(Graphics g,bool working,float tipX,float tipY,bool left,bool above,Font font){
        if(working){
          float sx=left?-1:1,sy=above?-1:1;
          PointF[] points=new PointF[]{new PointF(0,0),new PointF(1,23),new PointF(7,17),new PointF(11,26),new PointF(15,24),new PointF(11,15),new PointF(22,15)};
          for(int i=0;i<points.Length;i++)points[i]=new PointF(tipX+points[i].X*sx,tipY+points[i].Y*sy);
          using(var shape=new GraphicsPath()){
            shape.AddPolygon(points);
            using(var fill=new SolidBrush(Color.FromArgb(184,244,222)))g.FillPath(fill,shape);
            using(var edge=new Pen(Color.FromArgb(20,59,48),1.6f)){edge.LineJoin=LineJoin.Round;g.DrawPath(edge,shape);}
          }
          DrawPill(g,new RectangleF(left?tipX-94:tipX+28,above?tipY-31:tipY+5,64,27),"AI",font);
        }else DrawPill(g,new RectangleF(4,10,108,30),"UDB  AI",font);
  }
  public static void SavePreview(string path){
    using(var bitmap=new Bitmap(480,180,PixelFormat.Format32bppPArgb))
    using(var g=Graphics.FromImage(bitmap))
    using(var label=new Font("Segoe UI",10.0f,FontStyle.Regular))
    using(var text=new SolidBrush(Color.FromArgb(159,178,186)))
    using(var pointerFont=new Font("Segoe UI",8.2f,FontStyle.Bold)){
      g.Clear(Color.FromArgb(13,18,22));g.SmoothingMode=SmoothingMode.AntiAlias;
      g.TextRenderingHint=System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
      g.DrawString("Ready",label,text,new PointF(32,26));
      g.DrawString("Working",label,text,new PointF(252,26));
      g.TranslateTransform(28,66);DrawScene(g,false,4,4,false,false,pointerFont);
      g.ResetTransform();g.TranslateTransform(250,66);DrawScene(g,true,4,4,false,false,pointerFont);
      bitmap.Save(path,ImageFormat.Png);
    }
  }
  void RenderFrame(){
    if(!Visible||stopped)return;
    Rectangle area=Screen.FromPoint(target).WorkingArea;
    if(!badgeOnly&&!working&&activeTab)area=pageBounds;
    float scale=ScaleAt(target);int width=(int)Math.Ceiling(128*scale),height=(int)Math.Ceiling(52*scale);
    if(badgeOnly){width=(int)Math.Ceiling(64*scale);height=(int)Math.Ceiling(27*scale);}
    bool left=target.X+width>area.Right, above=target.Y+height>area.Bottom;
    float tipX=left?124:4,tipY=above?48:4;
    Point position=working?new Point(target.X-(int)(tipX*scale),target.Y-(int)(tipY*scale)):new Point(area.Right-(int)(144*scale),area.Bottom-(int)(66*scale));
    if(badgeOnly)position=target;
    using(var bitmap=new Bitmap(width,height,PixelFormat.Format32bppPArgb)){
      using(var g=Graphics.FromImage(bitmap)){
        g.Clear(Color.Transparent);g.SmoothingMode=SmoothingMode.AntiAlias;
        g.TextRenderingHint=System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
        g.ScaleTransform(scale,scale);
        if(badgeOnly)DrawPill(g,new RectangleF(1,1,62,25),"AI",font);
        else DrawScene(g,working,tipX,tipY,left,above,font);
      }
      IntPtr screen=GetDC(IntPtr.Zero),memory=IntPtr.Zero,image=IntPtr.Zero,old=IntPtr.Zero;
      try{
        memory=CreateCompatibleDC(screen);image=bitmap.GetHbitmap(Color.FromArgb(0));old=SelectObject(memory,image);
        var pos=new PointNative(position.X,position.Y);var size=new SizeNative(width,height);var origin=new PointNative(0,0);
        var blend=new Blend{Op=0,Flags=0,Alpha=255,Format=1};
        if(!UpdateLayeredWindow(Handle,screen,ref pos,ref size,memory,ref origin,0,ref blend,2))throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
      }finally{
        if(old!=IntPtr.Zero)SelectObject(memory,old);
        if(image!=IntPtr.Zero)DeleteObject(image);
        if(memory!=IntPtr.Zero)DeleteDC(memory);
        if(screen!=IntPtr.Zero)ReleaseDC(IntPtr.Zero,screen);
      }
    }
  }
  public UdbPointer():this(false){}
  UdbPointer(bool isBadge){
    badgeOnly=isBadge;
    AutoScaleMode=AutoScaleMode.None;FormBorderStyle=FormBorderStyle.None;ShowInTaskbar=false;TopMost=true;
    ClientSize=new Size(128,52);StartPosition=FormStartPosition.Manual;Location=new Point(-500,-500);
    if(badgeOnly)return;
    var reader=new Thread(()=>{try{string line;while((line=Console.ReadLine())!=null)queue.Enqueue(line);}finally{queue.Enqueue("stop");}});
    reader.IsBackground=true;reader.Start();timer.Interval=50;
    timer.Tick+=(sender,ev)=>{
      bool changed=false,probe=false;string line;
      while(queue.TryDequeue(out line)){
        if(line=="stop"){stopped=true;Close();return;}
        if(line=="hide"){enabled=false;Hide();foreach(var badge in badges.Values)badge.Hide();continue;}
        if(line=="ready"){enabled=true;working=false;changed=true;continue;}
        if(line=="probe"){probe=true;changed=true;continue;}
        string[] parts=line.Split(',');int x,y;
        if(parts.Length==2&&int.TryParse(parts[0],out x)&&int.TryParse(parts[1],out y)){
          enabled=true;target=new Point(x,y);working=true;lastAction=DateTime.UtcNow;changed=true;

        }
      }
      if(working&&(DateTime.UtcNow-lastAction).TotalMilliseconds>1800){working=false;changed=true;}
      if((DateTime.UtcNow-lastPoll).TotalMilliseconds>=350){
        lastPoll=DateTime.UtcNow;
        if(enabled)PollTabs();else activeTab=false;
        changed=true;
      }
      if(MayShow()){if(!Visible)Show();if(changed)RenderFrame();}else Hide();
      if(probe){Console.WriteLine(Visible?"painted":"hidden");Console.Out.Flush();}
    };
    timer.Start();Console.WriteLine("ready");Console.Out.Flush();
  }
  protected override void Dispose(bool disposing){if(disposing){timer.Dispose();foreach(var badge in badges.Values)badge.Dispose();badges.Clear();font.Dispose();}base.Dispose(disposing);}
}
'@
if ($PreviewPath) { [UdbPointer]::SavePreview($PreviewPath); exit }
[UdbPointer]::ConfigureDpi()
[System.Windows.Forms.Application]::Run((New-Object UdbPointer))
