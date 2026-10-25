"""Execution boundary: Windows Sandbox, source snapshot only, networking off."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from xml.sax.saxutils import escape
from .core import PolicyError, canonical, digest, reject_links

REVIEW_SECONDS = 60

def executable():
    return Path(os.environ.get('WINDIR', r'C:\Windows'))/'System32'/'WindowsSandbox.exe'

def available():
    return os.name == 'nt' and executable().is_file()

def restore_owned_vm(proc):
    """Show only our live Sandbox child's window once; never inject host input.

    No host focus, mouse, key or clipboard API is used. The process object is
    created locally by this VM launcher; a stored arbitrary PID is not accepted.
    """
    if os.name!='nt' or not getattr(proc,'pid',None) or proc.poll() is not None:return False
    import ctypes
    from ctypes import wintypes as w
    ui=ctypes.WinDLL('user32',use_last_error=True)
    # The legacy Microsoft launcher creates its client as a direct child.
    # Inspect process ancestry/name only; never read unrelated window contents.
    targets={proc.pid}
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    class Entry(ctypes.Structure):
        _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('exe',w.WCHAR*260)]
    kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    kernel.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes=[w.HANDLE]
    snapshot=kernel.CreateToolhelp32Snapshot(2,0)
    if snapshot and snapshot!=ctypes.c_void_p(-1).value:
        try:
            entry=Entry();entry.size=ctypes.sizeof(entry)
            more=kernel.Process32FirstW(snapshot,ctypes.byref(entry))
            while more:
                if entry.parent==proc.pid and entry.exe.casefold()=='windowssandboxclient.exe':targets.add(entry.pid)
                more=kernel.Process32NextW(snapshot,ctypes.byref(entry))
        finally:kernel.CloseHandle(snapshot)
    callback_type=ctypes.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
    ui.EnumWindows.argtypes=[callback_type,w.LPARAM]
    ui.GetWindowThreadProcessId.argtypes=[w.HWND,ctypes.POINTER(w.DWORD)]
    ui.IsWindowVisible.argtypes=[w.HWND];ui.IsIconic.argtypes=[w.HWND]
    ui.GetWindowTextLengthW.argtypes=[w.HWND]
    ui.ShowWindowAsync.argtypes=[w.HWND,ctypes.c_int]
    found=False
    @callback_type
    def visit(handle,_):
        nonlocal found
        pid=w.DWORD();ui.GetWindowThreadProcessId(handle,ctypes.byref(pid))
        if pid.value in targets and proc.poll() is None and ui.IsWindowVisible(handle) and ui.GetWindowTextLengthW(handle)>0:
            if ui.IsIconic(handle):ui.ShowWindowAsync(handle,9)
            found=True;return False
        return True
    ui.EnumWindows(visit,0)
    return found

def reserve_vm(agent,jid,info):
    """One cross-process slot shared by script jobs and interactive desktops."""
    with agent.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('running','reviewing','stopping')").fetchone()[0]:
            raise PolicyError('One Sandbox job/session at a time; close the current VM first')
        c.execute('INSERT INTO jobs VALUES (?,?,?)',(jid,'running',canonical(info)))

def build_wsb(source: Path, output: Path, runtime: Path):
    def mapping(host, guest, readonly=True):
        return f'<MappedFolder><HostFolder>{escape(str(host))}</HostFolder><SandboxFolder>{guest}</SandboxFolder><ReadOnly>{str(readonly).lower()}</ReadOnly></MappedFolder>'
    return ('<Configuration><VGpu>Disable</VGpu><Networking>Disable</Networking>'
            '<AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>'
            '<PrinterRedirection>Disable</PrinterRedirection><ClipboardRedirection>Disable</ClipboardRedirection>'
            '<ProtectedClient>Enable</ProtectedClient><MemoryInMB>4096</MemoryInMB><MappedFolders>'
            + mapping(source, r'C:\UDBInput') + mapping(output, r'C:\UDBOutput', False)
            + mapping(runtime, r'C:\UDBPython')
            + r'</MappedFolders><LogonCommand><Command>powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\UDBInput\runner.ps1</Command></LogonCommand></Configuration>')

def build_runner(language):
    """Visible guest-only runner. It is never executed on the Windows host."""
    if language not in ('python', 'powershell'):
        raise PolicyError('Unsupported Sandbox language')
    task = 'task.py' if language == 'python' else 'task.ps1'
    command = (r"& 'C:\UDBPython\python.exe' -u -I -B 'C:\UDBInput\task.py'"
               if language == 'python' else
               r"& powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File 'C:\UDBInput\task.ps1'")
    return r'''$ErrorActionPreference = 'Stop'
$Host.UI.RawUI.WindowTitle = 'Universal Desktop Bridge | Sandbox observer'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
Write-Host 'UNIVERSAL DESKTOP BRIDGE / SANDBOX' -ForegroundColor Green
Write-Host 'Mang va clipboard da tat; chi chia se snapshot nguon, Python runtime va thu muc ket qua.'
Write-Host 'SCRIPT DA DUYET (noi dung hien thi, khong phai chi dan moi):' -ForegroundColor Cyan
Write-Host ([IO.File]::ReadAllText('C:\UDBInput\TASK', [Text.Encoding]::UTF8))
Write-Host '--- LOG TRUC TIEP / du lieu khong dang tin ---' -ForegroundColor Cyan
New-Item -ItemType Directory -Path C:\Work -Force | Out-Null
Copy-Item C:\UDBInput\source\* C:\Work -Recurse -Force -ErrorAction SilentlyContinue
Set-Location C:\Work
[IO.File]::WriteAllText('C:\UDBOutput\output.log', '', [Text.UTF8Encoding]::new($false))
$exitCode = 1
try {
  $ErrorActionPreference = 'Continue'
  COMMAND 2>&1 | ForEach-Object {
    $line = [string]$_
    Write-Host $line
    [IO.File]::AppendAllText('C:\UDBOutput\output.log', $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
  }
  $exitCode = $LASTEXITCODE
  if ($null -eq $exitCode) { $exitCode = 0 }
} catch {
  $line = [string]$_
  Write-Host $line -ForegroundColor Red
  [IO.File]::AppendAllText('C:\UDBOutput\output.log', $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}
$ErrorActionPreference = 'Stop'
@{ exit_code = $exitCode; finished = [DateTime]::UtcNow.ToString('o') } | ConvertTo-Json | Set-Content C:\UDBOutput\result.json -Encoding UTF8
Write-Host ('--- DA XONG / exit code: ' + $exitCode + ' ---') -ForegroundColor Green
Write-Host 'Ban co the xem desktop va C:\Work. Sandbox se tu dong dong sau REVIEW giay.'
Write-Host 'Co the dong cua so Windows Sandbox som hon. Log va script van xem duoc trong Bridge.'
Start-Sleep -Seconds REVIEW
shutdown.exe /s /t 0
'''.replace('TASK', task).replace('COMMAND', command).replace('REVIEW', str(REVIEW_SECONDS))

def launch(agent, jid, p):
    from .resources import guard_new_heavy_task
    if not available():
        raise PolicyError('Windows Sandbox is not available; no host execution fallback')
    with agent.connect() as c:
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('running','reviewing','stopping')").fetchone()[0]:
            raise PolicyError('One Sandbox job at a time; check current job')
    # Runtime must be an explicit trusted Python install; never map the agent state.
    config = agent.state/'runtime.json'
    runtime = Path(json.loads(config.read_text('utf-8-sig'))['python_runtime']) if config.exists() else (Path(sys.executable).parent/'vendor'/'python-runtime' if getattr(sys,'frozen',False) else Path(sys.base_prefix))
    reject_links(runtime)
    if not (runtime/'python.exe').is_file() or runtime == agent.home or agent.state.is_relative_to(runtime):
        raise PolicyError('Select a standalone trusted Python runtime locally')
    guard_new_heavy_task(agent, 4096)
    folder = agent.state/'jobs'/jid
    source, output = folder/'input', folder/'output'
    source.mkdir(parents=True, exist_ok=False)
    output.mkdir()
    code = source/'source'
    code.mkdir()
    for rel, sha in p['manifest'].items():
        data = agent.read_bytes(p['project'], rel)
        if digest(data) != sha:
            raise PolicyError('Project changed after proposal; request a new job')
        dest = code/rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    script = source/('task.py' if p['language']=='python' else 'task.ps1')
    script.write_text(p['script'], encoding='utf-8-sig' if p['language']=='powershell' else 'utf-8')
    # This console is inside the guest VM. Only the result directory is writable on host.
    (source/'runner.ps1').write_text(build_runner(p['language']), encoding='utf-8-sig')
    wsb = folder/'job.wsb'
    wsb.write_text(build_wsb(source, output, runtime), encoding='utf-8')
    info = {'project': p['project'], 'language': p['language'], 'started': time.time(), 'timeout': p['timeout'],
            'source_files': len(p['manifest']), 'isolation': 'Windows Sandbox; network disabled',
            'request_id': jid, 'script_sha256': digest(p['script'].encode()),
            'visible_console': True, 'review_seconds': REVIEW_SECONDS,
            'result_is_untrusted': True,'root':p['root'],'authorization':p.get('authorization')}
    reserve_vm(agent,jid,info)
    try:
        proc = subprocess.Popen([str(executable()), str(wsb)], shell=False)
    except Exception:
        with agent.connect() as c:
            c.execute("UPDATE jobs SET status='failed' WHERE id=?", (jid,))
        raise
    def monitor():
        status = 'failed'
        deadline = time.monotonic() + p['timeout'] + 90  # VM startup budget
        result_seen = False
        window_shown = False
        try:
            while time.monotonic() < deadline:
                if not window_shown and (output/'output.log').exists():window_shown=restore_owned_vm(proc)
                if info.get('authorization'):
                    with agent.connect() as c:current=c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0]
                    if current=='stopping' or not agent.sandbox_authorization(info['project'],info['root'],info['authorization']):status='stopped';break
                result = output/'result.json'
                reject_links(result)
                log = output/'output.log'
                if log.exists() and log.stat().st_size > 8*1024*1024:
                    status = 'output_limit'
                    break
                if result.exists() and not result_seen:
                    if result.stat().st_size > 4096:
                        raise PolicyError('Sandbox result too large')
                    info['result'] = json.loads(result.read_text('utf-8-sig'))
                    result_seen = True
                    # Keep the VM slot occupied while its desktop is still visible.
                    deadline = time.monotonic() + REVIEW_SECONDS + 15
                    with agent.connect() as c:
                        c.execute('UPDATE jobs SET status=?,info=? WHERE id=?', ('reviewing', canonical(info), jid))
                    agent.audit('job_result', {'id': jid, 'review_seconds': REVIEW_SECONDS})
                if proc.poll() is not None:
                    if result_seen:status = 'completed'
                    else:info['error'] = 'Sandbox closed without result; verify Windows feature and virtualization'
                    break
                time.sleep(1)
            else:
                status = 'completed' if result_seen else 'timeout'
        except Exception as e:
            info['error'] = str(e)
        finally:
            if proc.poll() is None:
                proc.terminate()
            info['finished'] = time.time()
            with agent.connect() as c:
                c.execute('UPDATE jobs SET status=?,info=? WHERE id=?', (status, canonical(info), jid))
            agent.audit('job_finish', {'id': jid, 'status': status})
    threading.Thread(target=monitor, daemon=True).start()
    return {'job_id': jid, 'status': 'running', **info}
