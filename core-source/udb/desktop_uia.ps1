# Sandbox guest-only UI Automation fallback: scoped exclusively to the demo form.
# No arbitrary window selection, shell command, URL, elevation or host API.
param(
    [ValidateSet('click','type')][string]$Mode,
    [int]$X=0,[int]$Y=0,[string]$TextB64=''
)
$ErrorActionPreference='Stop'
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class UdbFindDemo {
  [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern IntPtr FindWindowW(string cls, string title);
  [DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern IntPtr OpenWindowStationW(string name,bool inherit,uint access);
  [DllImport("user32.dll",SetLastError=true)]
  public static extern bool SetProcessWindowStation(IntPtr station);
  [DllImport("user32.dll",SetLastError=true)]
  public static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
  [DllImport("user32.dll",SetLastError=true)]
  public static extern bool SetThreadDesktop(IntPtr desktop);
  [DllImport("user32.dll",SetLastError=true)]
  public static extern IntPtr GetThreadDesktop(uint tid);
  [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  public static extern bool GetUserObjectInformationW(IntPtr obj,int index,System.Text.StringBuilder name,int length,out uint needed);
  [DllImport("kernel32.dll")]
  public static extern uint GetCurrentThreadId();
  [DllImport("user32.dll",SetLastError=true)]
  public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  public static extern IntPtr FindWindowExW(IntPtr parent,IntPtr after,string cls,string title);
  [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  public static extern int GetClassNameW(IntPtr handle,System.Text.StringBuilder buffer,int size);
  [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
  public static extern int GetWindowTextW(IntPtr handle,System.Text.StringBuilder buffer,int size);
  [StructLayout(LayoutKind.Sequential)]
  public struct Rect {public int Left,Top,Right,Bottom;}
  [DllImport("user32.dll",SetLastError=true)]
  public static extern bool GetWindowRect(IntPtr handle,out Rect rect);
  [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",CharSet=CharSet.Unicode,SetLastError=true)]
  public static extern IntPtr SendText(IntPtr target,uint msg,IntPtr wParam,string payload,uint flags,uint timeout,out IntPtr result);
  [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",CharSet=CharSet.Unicode,SetLastError=true)]
  public static extern IntPtr SendPtr(IntPtr target,uint msg,IntPtr wParam,IntPtr payload,uint flags,uint timeout,out IntPtr result);
}
'@
    function Invoke-DemoNative {
        param([string]$Operation,[IntPtr]$Window,[int]$Px,[int]$Py,[string]$Value)
        if($Window -eq [IntPtr]::Zero){throw 'Demo window handle not available to native guest fallback'}
        $item=[UdbFindDemo]::FindWindowExW($Window,[IntPtr]::Zero,$null,$null)
        $editHandle=[IntPtr]::Zero
        $buttonHandle=[IntPtr]::Zero
        $found=0
        while($item -ne [IntPtr]::Zero -and $found -lt 80){
            $found++
            $cls=New-Object System.Text.StringBuilder(260)
            $title=New-Object System.Text.StringBuilder(260)
            [void][UdbFindDemo]::GetClassNameW($item,$cls,260)
            [void][UdbFindDemo]::GetWindowTextW($item,$title,260)
            $kind=$cls.ToString().ToLowerInvariant()
            if($kind.Contains('edit') -and $editHandle -eq [IntPtr]::Zero){
                $editHandle=$item
            }
            if($kind.Contains('button') -and $title.ToString() -eq 'Confirm'){
                $rect=[UdbFindDemo+Rect]::new()
                if([UdbFindDemo]::GetWindowRect($item,[ref]$rect) -and
                    $Px -ge $rect.Left -and $Px -lt $rect.Right -and
                    $Py -ge $rect.Top -and $Py -lt $rect.Bottom){
                    $buttonHandle=$item
                }
            }
            $item=[UdbFindDemo]::FindWindowExW($Window,$item,$null,$null)
        }
        [IntPtr]$outcome=[IntPtr]::Zero
        if($Operation -eq 'type'){
            if($editHandle -eq [IntPtr]::Zero){throw 'Demo-only native text edit not found'}
            $status=[UdbFindDemo]::SendText($editHandle,0x000C,[IntPtr]::Zero,$Value,0x0002,3000,[ref]$outcome)
            if($status -eq [IntPtr]::Zero -or $outcome -eq [IntPtr]::Zero){
                throw 'Demo-only WM_SETTEXT was refused'
            }
            Write-Output 'DEMO_NATIVE_SET_TEXT_OK'
        } elseif($Operation -eq 'click'){
            if($buttonHandle -eq [IntPtr]::Zero){throw 'Demo-only Confirm button not found at cursor coordinates'}
            $status=[UdbFindDemo]::SendPtr($buttonHandle,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero,0x0002,3000,[ref]$outcome)
            if($status -eq [IntPtr]::Zero){throw 'Demo-only BM_CLICK was refused'}
            Write-Output 'DEMO_NATIVE_CLICK_OK'
        } else { throw 'Demo-native fallback only handles type or Confirm' }
    }
    # Open the existing interactive guest station via the guest's current ACL.
    # No modification of ACLs, tokens, integrity levels, UAC or the host desktop.
    $winsta=[UdbFindDemo]::OpenWindowStationW('WinSta0',$false,0x037F)
    if($winsta -eq [IntPtr]::Zero){throw 'Cannot open guest interactive WinSta0'}
    if(-not [UdbFindDemo]::SetProcessWindowStation($winsta)){
        throw 'Cannot bind UI Automation child to guest window station'
    }
    $desktop=[UdbFindDemo]::OpenInputDesktop(0,$false,0x0187)
    if($desktop -eq [IntPtr]::Zero){throw 'Guest input desktop inaccessible'}
    if(-not [UdbFindDemo]::SetThreadDesktop($desktop)){
        # PowerShell may already own hidden windows. Accept only when its current
        # thread is already bound to the guest Default desktop; never force ACLs.
        $current=[UdbFindDemo]::GetThreadDesktop([UdbFindDemo]::GetCurrentThreadId())
        $name=New-Object System.Text.StringBuilder(128)
        [uint32]$needed=0
        if($current -eq [IntPtr]::Zero -or
           -not [UdbFindDemo]::GetUserObjectInformationW($current,2,$name,256,[ref]$needed) -or
           $name.ToString() -ne 'Default'){
            throw 'UI Automation child is not on guest Default desktop'
        }
    }
    $hwnd=[UdbFindDemo]::FindWindowW($null,'UDB Sandbox Pad - guest only')
    if($hwnd -ne [IntPtr]::Zero){
        $parent=[System.Windows.Automation.AutomationElement]::FromHandle($hwnd)
    }else{
        # An exact-name UIA root search handles visible windows without a
        # Win32 HWND accessible to this COM client. Still demo-only.
        $root=[System.Windows.Automation.AutomationElement]::RootElement
        $condition=[System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::NameProperty,'UDB Sandbox Pad - guest only')
        $parent=$root.FindFirst([System.Windows.Automation.TreeScope]::Children,$condition)
    }
    if($null -eq $parent -or $parent.Current.Name -ne 'UDB Sandbox Pad - guest only'){
        throw 'Demo-only UIA identity mismatch'
    }
    if($hwnd -eq [IntPtr]::Zero){$hwnd=[IntPtr]([int]$parent.Current.NativeWindowHandle)}
    $nodes=$parent.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                           [System.Windows.Automation.Condition]::TrueCondition)
    $edit=$null
    $target=$null
    for($i=0;$i -lt $nodes.Count;$i++){
        $item=$nodes.Item($i)
        $kind=$item.Current.ControlType.ProgrammaticName
        if($kind -eq 'ControlType.Edit' -and $null -eq $edit){$edit=$item}
        if($Mode -ne 'click'){continue}
        $rect=$item.Current.BoundingRectangle
        if($X -ge $rect.Left -and $X -lt $rect.Right -and
           $Y -ge $rect.Top -and $Y -lt $rect.Bottom -and
           ($kind -eq 'ControlType.Edit' -or $kind -eq 'ControlType.Button')){
            # Pick first matching interactive child; no arbitrary window action.
            if($null -eq $target){$target=$item}
        }
    }
    if($Mode -eq 'type'){
        $text=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($TextB64))
        if($null -eq $edit){
            Invoke-DemoNative -Operation type -Window $hwnd -Px $X -Py $Y -Value $text
            exit 0
        }
        $pattern=$edit.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
        if($null -eq $pattern){throw 'Demo edit does not expose ValuePattern'}
        ([System.Windows.Automation.ValuePattern]$pattern).SetValue($text)
        Write-Output 'UIA_SET_VALUE_OK'
    } else {
        if($null -eq $target){
            Invoke-DemoNative -Operation click -Window $hwnd -Px $X -Py $Y -Value ''
            exit 0
        }
        $kind=$target.Current.ControlType.ProgrammaticName
        if($kind -eq 'ControlType.Edit'){
            $target.SetFocus()
            Write-Output 'UIA_EDIT_FOCUS_OK'
        } elseif($kind -eq 'ControlType.Button' -and $target.Current.Name -eq 'Confirm'){
            $pattern=$target.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            if($null -eq $pattern){throw 'Demo button does not expose InvokePattern'}
            ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
            Write-Output 'UIA_INVOKE_CONFIRM_OK'
        } else {throw 'UIA action not allowed on this demo control'}
    }
    exit 0
} catch {
    [Console]::Error.WriteLine('UIA_FAILED '+[string]$_.Exception.Message)
    exit 1
}
