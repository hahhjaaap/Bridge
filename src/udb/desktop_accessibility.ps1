# Persistent Windows Sandbox UIA worker. No host process, network or shell commands.
param([switch]$Persistent,[string]$PayloadB64='')
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
if ([Security.Principal.WindowsIdentity]::GetCurrent().Name.Split('\')[-1] -ne 'WDAGUtilityAccount') {
    throw 'Sandbox guest identity required'
}
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class UdbForeground {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr FindWindowW(string cls,string title);
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern IntPtr OpenWindowStationW(string name,bool inherit,uint access);
    [DllImport("user32.dll",SetLastError=true)] public static extern bool SetProcessWindowStation(IntPtr station);
    [DllImport("user32.dll",SetLastError=true)] public static extern IntPtr OpenInputDesktop(uint flags,bool inherit,uint access);
    [DllImport("user32.dll",SetLastError=true)] public static extern bool SetThreadDesktop(IntPtr desktop);
    [DllImport("user32.dll",SetLastError=true)] public static extern IntPtr GetThreadDesktop(uint threadId);
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)] public static extern bool GetUserObjectInformationW(IntPtr obj,int index,System.Text.StringBuilder name,int length,out uint needed);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
    [DllImport("user32.dll",CharSet=CharSet.Unicode,SetLastError=true)]
    public static extern int GetClassNameW(IntPtr hWnd,System.Text.StringBuilder text,int maxCount);
    [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",CharSet=CharSet.Unicode,SetLastError=true)]
    public static extern IntPtr SendText(IntPtr hWnd,uint msg,IntPtr wp,string text,uint flags,uint timeout,out IntPtr result);
    [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",CharSet=CharSet.Unicode,SetLastError=true)]
    public static extern IntPtr ReadText(IntPtr hWnd,uint msg,IntPtr wp,System.Text.StringBuilder text,uint flags,uint timeout,out IntPtr result);
    [DllImport("user32.dll",EntryPoint="SendMessageTimeoutW",SetLastError=true)]
    public static extern IntPtr SendPtr(IntPtr hWnd,uint msg,IntPtr wp,IntPtr lp,uint flags,uint timeout,out IntPtr result);
}
'@
$station=[UdbForeground]::OpenWindowStationW('WinSta0',$false,0x037F)
if ($station -eq [IntPtr]::Zero) { throw 'Guest interactive window station unavailable' }
if (-not [UdbForeground]::SetProcessWindowStation($station)) { throw 'Cannot bind guest UIA process to WinSta0' }
$inputDesktop=[UdbForeground]::OpenInputDesktop(0,$false,0x0187)
if ($inputDesktop -eq [IntPtr]::Zero) { throw 'Guest input desktop inaccessible' }
if (-not [UdbForeground]::SetThreadDesktop($inputDesktop)) {
    $current=[UdbForeground]::GetThreadDesktop([UdbForeground]::GetCurrentThreadId())
    $desktopName=[Text.StringBuilder]::new(128)
    [uint32]$needed=0
    if ($current -eq [IntPtr]::Zero -or
        -not [UdbForeground]::GetUserObjectInformationW($current,2,$desktopName,256,[ref]$needed) -or
        $desktopName.ToString() -ne 'Default') {
        throw 'UIA helper cannot use guest Default desktop'
    }
}
$script:cachedTop=$null
$script:cachedTitle=''
$script:cachedHandle=[IntPtr]::Zero
$script:cachedElements=@{}
$script:duplicateIds=@{}
function NativeText([IntPtr]$native) {
    $buffer=[Text.StringBuilder]::new(2002)
    [IntPtr]$count=[IntPtr]::Zero
    $sent=[UdbForeground]::ReadText($native,0x000D,[IntPtr]2002,$buffer,2,1200,[ref]$count)
    if ($sent -eq [IntPtr]::Zero) { throw 'Guest native text read timed out or was denied' }
    return $buffer.ToString()
}
function Info($element) {
    try {
        $c=$element.Current
        $bounds=$c.BoundingRectangle
        $typ=[string]$c.ControlType.ProgrammaticName
        if ($typ.StartsWith('ControlType.')) { $typ=$typ.Substring(12) }
        $native=[IntPtr]$c.NativeWindowHandle
        $nativeClass=''
        if ($native -ne [IntPtr]::Zero) {
            $classBuffer=[Text.StringBuilder]::new(260)
            [void][UdbForeground]::GetClassNameW($native,$classBuffer,260)
            $nativeClass=$classBuffer.ToString()
        }
        $text=''
        if ($typ -eq 'Edit' -or $typ -eq 'Document') {
            $pattern=$null
            if ($element.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) {
                if (-not $c.IsPassword) { $text=[string]$pattern.Current.Value }
            } elseif ($native -ne [IntPtr]::Zero -and $nativeClass -match 'EDIT|RICHEDIT' -and -not $c.IsPassword) {
                try { $text=NativeText $native } catch {}
            }
        }
        return [pscustomobject]@{
            name=([string]$c.Name).Substring(0,[Math]::Min(120,([string]$c.Name).Length))
            automation_id=([string]$c.AutomationId).Substring(0,[Math]::Min(120,([string]$c.AutomationId).Length))
            control_type=$typ
            native_handle=$native
            native_class=$nativeClass
            text=$text.Substring(0,[Math]::Min(512,$text.Length))
            enabled=[bool]$c.IsEnabled
            offscreen=[bool]$c.IsOffscreen
            bounds=@([int]$bounds.Left,[int]$bounds.Top,[int]$bounds.Width,[int]$bounds.Height)
            element=$element
        }
    } catch { return $null }
}
function Gather($top,[int]$limit) {
    $queue=[Collections.Generic.Queue[object]]::new()
    $queue.Enqueue(@{element=$top;depth=0})
    $items=[Collections.Generic.List[object]]::new()
    $visited=0
    while ($queue.Count -gt 0 -and $visited -lt $limit) {
        $entry=$queue.Dequeue()
        $visited++
        $info=Info $entry.element
        if ($null -ne $info -and -not $info.offscreen) { $items.Add($info) }
        if ($entry.depth -ge 6) { continue }
        try {
            $children=$entry.element.FindAll(
                [Windows.Automation.TreeScope]::Children,
                [Windows.Automation.Condition]::TrueCondition)
            for($i=0;$i -lt $children.Count -and $i -lt 70 -and ($queue.Count+$visited) -lt ($limit+60);$i++) {
                $queue.Enqueue(@{element=$children.Item($i);depth=($entry.depth+1)})
            }
        } catch {}
    }
    return $items.ToArray()
}
function SelectMatches($items,[string]$wantedName,[string]$wantedId,[string]$wantedType) {
    return @($items | Where-Object {
        (-not $wantedName -or $_.name -ceq $wantedName) -and
        (-not $wantedId -or $_.automation_id -ceq $wantedId) -and
        (-not $wantedType -or $_.control_type -ceq $wantedType)
    })
}
function Handle([string]$base64) {
    $req=([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($base64))|ConvertFrom-Json)
    $mode=[string]$req.mode
    $name=[string]$req.name
    $id=[string]$req.automation_id
    $typ=[string]$req.control_type
    $titleWanted=[string]$req.window_title
    $wantedHandle=[IntPtr]::Zero
    if ($null -ne $req.window_handle) { $wantedHandle=[IntPtr][long]$req.window_handle }
    $value=[string]$req.value
    $seconds=[int]$req.timeout_seconds
    if ($mode -notin @('snapshot','wait_for','element_invoke','element_set_text','window_focus')) { throw 'Unsupported guest UIA action' }
    if ($name.Length -gt 120 -or $id.Length -gt 120 -or $typ.Length -gt 80 -or $titleWanted.Length -gt 160 -or $value.Length -gt 2000) { throw 'UIA request too large' }
    if ($mode -notin @('snapshot','window_focus') -and -not ($name -or $id -or $typ)) { throw 'Name, automation_id or control_type required' }
    if ($seconds -lt 1 -or $seconds -gt 12) { $seconds=6 }
    $reuse=($mode -ne 'snapshot' -and $script:cachedTop -ne $null -and
            (($wantedHandle -ne [IntPtr]::Zero -and $wantedHandle -eq $script:cachedHandle) -or
             ($wantedHandle -eq [IntPtr]::Zero -and (-not $titleWanted -or $titleWanted -ceq $script:cachedTitle))))
    if ($reuse) {
        $top=$script:cachedTop
        try { $windowTitle=[string]$top.Current.Name }
        catch { $reuse=$false }
    }
    if (-not $reuse) {
        $hwnd=if ($wantedHandle -ne [IntPtr]::Zero) { $wantedHandle } elseif ($titleWanted) { [UdbForeground]::FindWindowW($null,$titleWanted) } else { [UdbForeground]::GetForegroundWindow() }
        if ($hwnd -eq [IntPtr]::Zero) { throw 'Guest window is not accessible' }
        $top=[Windows.Automation.AutomationElement]::FromHandle($hwnd)
        if ($null -eq $top) { throw 'Guest window UIA unavailable' }
        $windowTitle=[string]$top.Current.Name
        $script:cachedTop=$top
        $script:cachedTitle=$windowTitle
        $script:cachedHandle=$hwnd
    }
    if ($mode -eq 'window_focus') {
        try { $top.SetFocus() } catch {}
        if ([UdbForeground]::GetForegroundWindow() -ne $script:cachedHandle) {
            $focusable=@(Gather $top 240 | Where-Object { $_.enabled -and -not $_.offscreen -and $_.element.Current.IsKeyboardFocusable } | Select-Object -First 5)
            foreach($candidate in $focusable) {
                try { $candidate.element.SetFocus() } catch { continue }
                if ([UdbForeground]::GetForegroundWindow() -eq $script:cachedHandle) { break }
            }
        }
        if ([UdbForeground]::GetForegroundWindow() -ne $script:cachedHandle) { throw 'Windows denied foreground activation through UIA' }
        return @{ok=$true;action='window_focus';verified=$true}
    }
    if ($mode -eq 'snapshot') {
        $items=@(Gather $top 240)
        $script:cachedElements=@{}
        $script:duplicateIds=@{}
        foreach($item in $items) {
            $key=[string]$item.automation_id
            if (-not $key) { continue }
            if ($script:cachedElements.ContainsKey($key)) {
                [void]$script:cachedElements.Remove($key)
                $script:duplicateIds[$key]=$true
            } elseif (-not $script:duplicateIds.ContainsKey($key)) {
                $script:cachedElements[$key]=$item.element
            }
        }
        $rows=@($items | Select-Object -First 90 | ForEach-Object {
            @{name=$_.name;automation_id=$_.automation_id;control_type=$_.control_type;
              native_class=$_.native_class;native_handle_present=($_.native_handle -ne [IntPtr]::Zero);
              enabled=$_.enabled;bounds=$_.bounds;text=$_.text}
        })
        return @{ok=$true;active_window=$windowTitle;elements=$rows;
                 scanned=$items.Count;truncated=($items.Count -gt 90)}
    }
    $deadline=[DateTime]::UtcNow.AddSeconds($(if($mode -eq 'wait_for'){$seconds}else{1}))
    $matches=@()
    if ($id -and $script:cachedElements.ContainsKey($id)) {
        $candidate=Info $script:cachedElements[$id]
        if ($null -ne $candidate -and -not $candidate.offscreen -and
            (-not $name -or $candidate.name -ceq $name) -and
            (-not $typ -or $candidate.control_type -ceq $typ)) {
            $matches=@($candidate)
        }
    }
    if ($matches.Count -eq 0) {
        do {
            $matches=@(SelectMatches @(Gather $top 240) $name $id $typ)
            if ($matches.Count -gt 0 -or $mode -ne 'wait_for') { break }
            Start-Sleep -Milliseconds 180
        } while ([DateTime]::UtcNow -lt $deadline)
    }
    if ($matches.Count -eq 0) { throw ('UIA element not found in '+$windowTitle+': '+$name+'/'+$id+'/'+$typ) }
    if ($matches.Count -gt 1) { throw 'Ambiguous UIA element; specify automation_id or type' }
    $target=$matches[0]
    if ($mode -eq 'wait_for') {
        return @{ok=$true;found=$true;active_window=$windowTitle;
                 element=@{name=$target.name;automation_id=$target.automation_id;
                           control_type=$target.control_type;enabled=$target.enabled;bounds=$target.bounds}}
    }
    if (-not $target.enabled) { throw 'UIA target is disabled' }
    $pattern=$null
    if ($mode -eq 'element_invoke') {
        if ($target.element.TryGetCurrentPattern([Windows.Automation.InvokePattern]::Pattern,[ref]$pattern)) {
            $pattern.Invoke()
        } elseif ($target.element.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern,[ref]$pattern)) {
            $pattern.Toggle()
          } else {
            $native=[IntPtr]$target.native_handle
            if ($native -eq [IntPtr]::Zero -or $target.native_class -notmatch 'BUTTON') {
                throw 'No UIA invoke pattern or guest native Button handle'
            }
            [IntPtr]$outcome=[IntPtr]::Zero
            $delivered=[UdbForeground]::SendPtr($native,0x00F5,[IntPtr]::Zero,[IntPtr]::Zero,2,1200,[ref]$outcome)
            if ($delivered -eq [IntPtr]::Zero) { throw 'Guest native BM_CLICK not delivered' }
            return @{ok=$true;action='invoke';method='native_message';verified=$false;
                     name=$target.name;automation_id=$target.automation_id}
        }
        return @{ok=$true;action='invoke';method='uia_pattern';verified=$false;name=$target.name;automation_id=$target.automation_id}
    }
    if ($target.element.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) {
        if ($pattern.Current.IsReadOnly) { throw 'UIA target is read-only' }
        $pattern.SetValue($value)
        if ([string]$pattern.Current.Value -cne $value) { throw 'UIA text verification failed' }
        } else {
        $native=[IntPtr]$target.native_handle
        if ($native -eq [IntPtr]::Zero -or $target.native_class -notmatch 'EDIT|RICHEDIT') {
            throw 'No UIA ValuePattern or guest native Edit handle'
        }
        [IntPtr]$outcome=[IntPtr]::Zero
        $delivered=[UdbForeground]::SendText($native,0x000C,[IntPtr]::Zero,$value,2,1200,[ref]$outcome)
        if ($delivered -eq [IntPtr]::Zero -or $outcome -eq [IntPtr]::Zero) { throw 'Guest native WM_SETTEXT not acknowledged' }
        if ((NativeText $native) -cne $value) { throw 'Guest native text verification failed' }
        return @{ok=$true;action='set_text';method='native_message';verified=$true;
                 name=$target.name;automation_id=$target.automation_id}
    }
    return @{ok=$true;action='set_text';method='uia_pattern';verified=$true;
             name=$target.name;automation_id=$target.automation_id}
}
function Emit($message) {
    try {
        $value=Handle $message
        [Console]::Out.WriteLine('RES:'+($value|ConvertTo-Json -Compress -Depth 6))
    } catch {
        [Console]::Out.WriteLine('ERR:'+[string]$_.Exception.Message.Substring(0,[Math]::Min(400,[string]$_.Exception.Message.Length)))
    }
    [Console]::Out.Flush()
}
if ($Persistent) {
    [Console]::Out.WriteLine('READY')
    [Console]::Out.Flush()
    while ($null -ne ($line=[Console]::ReadLine())) {
        if ($line.Length -gt 18000) { [Console]::Out.WriteLine('ERR:Oversized UIA request');continue }
        Emit $line
    }
} else {
    if (-not $PayloadB64) { throw 'Missing UIA request' }
    Emit $PayloadB64
}