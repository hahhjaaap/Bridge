# Fixed current-user UI Automation helper. Caller checks a local owner grant.
# No arbitrary scripts/selectors, password reads, clipboard, elevation or input hooks.
$ErrorActionPreference='Stop'
[Console]::InputEncoding=[Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
function Observe-State([scriptblock]$Check) {
    $timer=[Diagnostics.Stopwatch]::StartNew()
    do {
        if(& $Check){return $true}
        if($timer.ElapsedMilliseconds -ge 500){return $false}
        Start-Sleep -Milliseconds 25
    } while($true)
}
try {
    Add-Type -AssemblyName UIAutomationClient
    Add-Type -AssemblyName UIAutomationTypes
    $raw=[Console]::In.ReadToEnd()
    if($raw.Length -gt 100000){throw 'Request too large'}
    $r=$raw|ConvertFrom-Json
    if($r.mode -notin @('snapshot','set_value','invoke','uia_focus','wait_for','read_text','set_checked','select','expand','collapse','scroll_into_view')){throw 'Unsupported UIA mode'}
    $root=[Windows.Automation.AutomationElement]::FromHandle([IntPtr][long]$r.hwnd)
    if(!$root -or $root.Current.ProcessId -ne [int]$r.pid){throw 'Observed window changed'}
    # Initialize AutomationElement before registering the standard .NET proxies.
    # Without these, Win32/WinForms controls can appear as generic Panes and
    # fail to expose ValuePattern/InvokePattern in this short-lived client.
    $providers=[Reflection.Assembly]::Load('UIAutomationClientsideProviders, Version=4.0.0.0, Culture=neutral, PublicKeyToken=31bf3856ad364e35')
    [Windows.Automation.ClientSettings]::RegisterClientSideProviderAssembly($providers.GetName())
    if($r.mode -eq 'snapshot') {
        $walker=[Windows.Automation.TreeWalker]::ControlViewWalker
        $queue=[Collections.Generic.Queue[object]]::new();$queue.Enqueue($root)
        $rows=[Collections.Generic.List[object]]::new()
        while($queue.Count -gt 0 -and $rows.Count -lt 200) {
            $element=$queue.Dequeue();$c=$element.Current;$rect=$c.BoundingRectangle
            if($c.IsPassword) {
                $rows.Add(@{name='<password>';password=$true;automation_id=$c.AutomationId})
            } else {
                $label=[string]$c.Name
                if($label.Length -gt 500){$label=$label.Substring(0,500)}
                $coords=@($rect.X,$rect.Y,$rect.Width,$rect.Height)
                $invalid=@($coords|Where-Object {[double]::IsNaN($_) -or [double]::IsInfinity($_)})
                $bounds=if($rect.IsEmpty -or $invalid.Count){@()}else{$coords}
                $patterns=@($element.GetSupportedPatterns() | ForEach-Object { $_.ProgrammaticName -replace 'PatternIdentifiers.Pattern$','' })
                $state=@{name=$label;automation_id=$c.AutomationId;control_type=$c.ControlType.ProgrammaticName;
                    enabled=$c.IsEnabled;offscreen=$c.IsOffscreen;bounds=$bounds;patterns=$patterns}
                $p=$null
                if($element.TryGetCurrentPattern([Windows.Automation.TogglePattern]::Pattern,[ref]$p)){$state.toggle_state=$p.Current.ToggleState.ToString()}
                $p=$null
                if($element.TryGetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern,[ref]$p)){$state.selected=$p.Current.IsSelected}
                $p=$null
                if($element.TryGetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern,[ref]$p)){$state.expanded=$p.Current.ExpandCollapseState.ToString()}
                $p=$null
                if($element.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$p)){$state.readonly=$p.Current.IsReadOnly}
                $rows.Add($state)
                $child=$walker.GetFirstChild($element);$count=0
                while($child -and $queue.Count -lt 400 -and $count -lt 100) {
                    $queue.Enqueue($child);$child=$walker.GetNextSibling($child);$count++
                }
            }
        }
        @{ok=$true;elements=$rows.ToArray();truncated=($queue.Count -gt 0)}|ConvertTo-Json -Depth 7 -Compress
        exit 0
    }
    $conditions=[Collections.Generic.List[Windows.Automation.Condition]]::new()
    if($r.automation_id){$conditions.Add([Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::AutomationIdProperty,[string]$r.automation_id))}
    if($r.name){$conditions.Add([Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::NameProperty,[string]$r.name))}
    if($r.control_type) {
        $types=@{Edit=[Windows.Automation.ControlType]::Edit;Button=[Windows.Automation.ControlType]::Button;
            ComboBox=[Windows.Automation.ControlType]::ComboBox;CheckBox=[Windows.Automation.ControlType]::CheckBox;
            Text=[Windows.Automation.ControlType]::Text;Document=[Windows.Automation.ControlType]::Document;
            List=[Windows.Automation.ControlType]::List;ListItem=[Windows.Automation.ControlType]::ListItem;
            Tree=[Windows.Automation.ControlType]::Tree;TreeItem=[Windows.Automation.ControlType]::TreeItem;
            RadioButton=[Windows.Automation.ControlType]::RadioButton;Tab=[Windows.Automation.ControlType]::Tab;
            TabItem=[Windows.Automation.ControlType]::TabItem;MenuItem=[Windows.Automation.ControlType]::MenuItem;
            Pane=[Windows.Automation.ControlType]::Pane;Group=[Windows.Automation.ControlType]::Group;
            Slider=[Windows.Automation.ControlType]::Slider;ScrollBar=[Windows.Automation.ControlType]::ScrollBar;
            Hyperlink=[Windows.Automation.ControlType]::Hyperlink;DataGrid=[Windows.Automation.ControlType]::DataGrid;
            DataItem=[Windows.Automation.ControlType]::DataItem;Custom=[Windows.Automation.ControlType]::Custom;
            Image=[Windows.Automation.ControlType]::Image;ToolBar=[Windows.Automation.ControlType]::ToolBar;
            Menu=[Windows.Automation.ControlType]::Menu;Window=[Windows.Automation.ControlType]::Window}
        $kind=([string]$r.control_type) -creplace '^ControlType\.',''
        if(!$types.ContainsKey($kind)){throw 'Unsupported control type'}
        $conditions.Add([Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::ControlTypeProperty,$types[$kind]))
    }
    if($conditions.Count -eq 0) {
        if($r.mode -ne 'uia_focus'){throw 'Provide an exact control ID or name'}
        $target=$root
    } else {
        $condition=if($conditions.Count -eq 1){$conditions[0]}else{[Windows.Automation.AndCondition]::new($conditions.ToArray())}
        if($r.mode -eq 'wait_for') {
            if(!$r.automation_id -and !$r.name){throw 'wait_for needs exact ID or name'}
            if([int]$r.wait_ms -lt 0 -or [int]$r.wait_ms -gt 10000 -or $r.condition -notin @('exists','absent','enabled')){throw 'Invalid wait options'}
            $timer=[Diagnostics.Stopwatch]::StartNew();$matched=$false;$observed=$null
            do {
                $found=$root.FindAll([Windows.Automation.TreeScope]::Descendants,$condition)
                if($found.Count -gt 1){throw 'Wait selector is ambiguous'}
                if($found.Count -eq 1 -and $found[0].Current.IsPassword){throw 'Password controls are refused'}
                $matched=if($r.condition -eq 'absent'){$found.Count -eq 0}elseif($r.condition -eq 'enabled'){$found.Count -eq 1 -and $found[0].Current.IsEnabled}else{$found.Count -eq 1}
                if($matched -and $found.Count -eq 1){$observed=@{automation_id=$found[0].Current.AutomationId;name=$found[0].Current.Name;control_type=$found[0].Current.ControlType.ProgrammaticName}}
                if($matched -or $timer.ElapsedMilliseconds -ge [int]$r.wait_ms){break}
                Start-Sleep -Milliseconds 100
            } while($true)
            @{ok=$true;matched=$matched;timed_out=(!$matched);elapsed_ms=$timer.ElapsedMilliseconds;element=$observed}|ConvertTo-Json -Depth 4 -Compress
            exit 0
        }
        $matches=$root.FindAll([Windows.Automation.TreeScope]::Descendants,$condition)
        if($matches.Count -ne 1){throw 'Selector must match exactly one control'}
        $target=$matches[0]
    }
    if($target.Current.IsPassword){throw 'Password controls are refused'}
    if($r.mode -eq 'read_text') {
        if(($r.limit -isnot [int] -and $r.limit -isnot [long]) -or $r.limit -lt 1 -or $r.limit -gt 5000){throw 'Invalid text limit'}
        $p=$null
        if($target.TryGetCurrentPattern([Windows.Automation.TextPattern]::Pattern,[ref]$p)) {
            $value=$p.DocumentRange.GetText([int]$r.limit+1);$method='TextPattern'
        } elseif($target.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$p)) {
            $value=[string]$p.Current.Value;$method='ValuePattern'
        } else {throw 'Text/Value pattern unsupported'}
        $truncated=$value.Length -gt [int]$r.limit
        if($truncated){
            $count=[int]$r.limit
            if($count -gt 0 -and [char]::IsHighSurrogate($value[$count-1]) -and [char]::IsLowSurrogate($value[$count])){$count--}
            $value=$value.Substring(0,$count)
        }
        @{ok=$true;text=$value;truncated=$truncated;method=$method}|ConvertTo-Json -Depth 3 -Compress
        exit 0
    }
    if(!$target.Current.IsEnabled){throw 'Disabled control'}
    if($r.mode -eq 'set_checked') {
        if($r.checked -isnot [bool]){throw 'checked must be boolean'}
        $p=$target.GetCurrentPattern([Windows.Automation.TogglePattern]::Pattern)
        $desired=if($r.checked){[Windows.Automation.ToggleState]::On}else{[Windows.Automation.ToggleState]::Off}
        $changed=$p.Current.ToggleState -ne $desired
        if($changed){$p.Toggle()}
        if(!(Observe-State {$p.Current.ToggleState -eq $desired})){throw 'State mismatch; inspect before retrying'}
        @{ok=$true;checked=[bool]$r.checked;changed=$changed;replay_safe=$false}|ConvertTo-Json -Compress
    } elseif($r.mode -eq 'select') {
        if($r.text) {
            if(([string]$r.text).Length -gt 500){throw 'Selection label too large'}
            $label=[Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::NameProperty,[string]$r.text)
            $selectable=[Windows.Automation.PropertyCondition]::new([Windows.Automation.AutomationElement]::IsSelectionItemPatternAvailableProperty,$true)
            $items=$target.FindAll([Windows.Automation.TreeScope]::Descendants,[Windows.Automation.AndCondition]::new($label,$selectable))
            if($items.Count -ne 1){throw 'Selection label must match one descendant'}
            $target=$items[0]
            if($target.Current.ProcessId -ne [int]$r.pid -or $target.Current.IsPassword -or !$target.Current.IsEnabled){throw 'Selection target refused'}
        }
        $p=$target.GetCurrentPattern([Windows.Automation.SelectionItemPattern]::Pattern)
        $already=$p.Current.IsSelected
        if(!$already){$p.Select()}
        if(!(Observe-State {$p.Current.IsSelected})){throw 'Selection readback mismatch'}
        @{ok=$true;selected=$true;already_set=$already;replay_safe=$false}|ConvertTo-Json -Compress
    } elseif($r.mode -in @('expand','collapse')) {
        $p=$target.GetCurrentPattern([Windows.Automation.ExpandCollapsePattern]::Pattern)
        $desired=if($r.mode -eq 'expand'){[Windows.Automation.ExpandCollapseState]::Expanded}else{[Windows.Automation.ExpandCollapseState]::Collapsed}
        $changed=$p.Current.ExpandCollapseState -ne $desired
        if($changed){if($r.mode -eq 'expand'){$p.Expand()}else{$p.Collapse()}}
        if(!(Observe-State {$p.Current.ExpandCollapseState -eq $desired})){throw 'Expansion readback mismatch'}
        @{ok=$true;expanded=($r.mode -eq 'expand');changed=$changed;replay_safe=$false}|ConvertTo-Json -Compress
    } elseif($r.mode -eq 'scroll_into_view') {
        $p=$target.GetCurrentPattern([Windows.Automation.ScrollItemPattern]::Pattern)
        $p.ScrollIntoView()
        $visible=Observe-State {!$target.Current.IsOffscreen}
        @{ok=$true;requested=$true;offscreen=(!$visible);replay_safe=$false}|ConvertTo-Json -Compress
    } elseif($r.mode -eq 'uia_focus') {
        $target.SetFocus();@{ok=$true;focus_requested=$true}|ConvertTo-Json -Compress
    } elseif($r.mode -eq 'set_value') {
        if(([string]$r.text).Length -gt 5000){throw 'Text too large'}
        $pattern=$null
        if($target.TryGetCurrentPattern([Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) {
            if($pattern.Current.IsReadOnly){throw 'Read-only control'}
            $pattern.SetValue([string]$r.text)
            if($pattern.Current.Value -cne [string]$r.text){throw 'Value readback mismatch'}
            $method='ValuePattern'
        } else {
            # Multiline native Edit controls can expose TextPattern without ValuePattern.
            # A fixed, timeout-bounded app-local message is allowed only to that
            # same-PID/root Edit handle. No arbitrary message/HWND is accepted.
            Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class UDBEdit {
 [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flag);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, StringBuilder b, int n);
 [DllImport("user32.dll", EntryPoint="GetWindowLongW")] public static extern int GetStyle(IntPtr h, int index);
 [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern IntPtr SendMessageTimeout(IntPtr h, uint m, UIntPtr w, string s, uint flags, uint ms, out UIntPtr result);
 [DllImport("user32.dll", EntryPoint="SendMessageTimeoutW", CharSet=CharSet.Unicode)] public static extern IntPtr ReadText(IntPtr h, uint m, UIntPtr w, StringBuilder b, uint flags, uint ms, out UIntPtr result);
}
'@
            $handle=[IntPtr][long]$target.Current.NativeWindowHandle
            $class=[Text.StringBuilder]::new(256);[UDBEdit]::GetClassName($handle,$class,256)>$null
            [uint32]$ownerPid=0;[UDBEdit]::GetWindowThreadProcessId($handle,[ref]$ownerPid)>$null
            if($class.ToString() -cne 'Edit' -or $ownerPid -ne [int]$r.pid -or
                [UDBEdit]::GetAncestor($handle,2) -ne [IntPtr][long]$r.hwnd -or
                ([UDBEdit]::GetStyle($handle,-16) -band 32)) {throw 'Native Edit fallback refused'}
            [UIntPtr]$ack=[UIntPtr]::Zero
            if([UDBEdit]::SendMessageTimeout($handle,12,[UIntPtr]::Zero,[string]$r.text,2,1000,[ref]$ack) -eq [IntPtr]::Zero){throw 'Edit message refused'}
            $read=[Text.StringBuilder]::new(5001)
            if([UDBEdit]::ReadText($handle,13,([UIntPtr]::new([uint32]5001)),$read,2,1000,[ref]$ack) -eq [IntPtr]::Zero){throw 'Edit readback refused'}
            if($read.ToString() -cne [string]$r.text){throw 'Edit readback mismatch'}
            $method='same_process_root_Edit_message'
        }
        @{ok=$true;verified_control_value=$true;characters=([string]$r.text).Length;method=$method}|ConvertTo-Json -Compress
    } else {
        $pattern=$target.GetCurrentPattern([Windows.Automation.InvokePattern]::Pattern)
        $pattern.Invoke();@{ok=$true;invoked=$true;verified_application_result=$false}|ConvertTo-Json -Compress
    }
} catch {
    @{ok=$false;error='UI Automation failed: control changed, selector ambiguous, pattern unsupported, or access refused.'}|ConvertTo-Json -Compress
    exit 1
}
