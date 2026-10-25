"""Host broker for a locally approved guest desktop. Never injects host input."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import threading
import time
import uuid
from xml.sax.saxutils import escape
import zlib
from .core import PolicyError, canonical, digest, reject_links
from .sandbox import available, executable, reserve_vm, restore_owned_vm
from .desktop_guest import DEMO, validate_steps

BUSY=('running','reviewing','stopping')
PNG_LIMIT=8*1024*1024
_OWNED={}
_OWNED_LOCK=threading.RLock()


def close_owned(agent):
    with _OWNED_LOCK:owned=list(_OWNED.items())
    for sid,(owner,proc) in owned:
        if owner.home!=agent.home:continue
        stop(owner,sid)
        if proc.poll() is None:proc.terminate()
        proc.wait(timeout=5)
        # GUI exit must not depend on a daemon monitor committing in time.
        with owner.connect() as c:
            row=c.execute('SELECT status,info FROM jobs WHERE id=?',(sid,)).fetchone()
            if row and row['status'] in BUSY:
                info=json.loads(row['info']);info['finished']=time.time()
                c.execute("UPDATE jobs SET status='stopped',info=? WHERE id=?",(canonical(info),sid))
        with _OWNED_LOCK:_OWNED.pop(sid,None)


def output_bytes(output):
    total=0;count=0
    for item in output.iterdir():
        reject_links(item)
        entries=item.iterdir() if item.is_dir() and item.name=='replies' else [item]
        for entry in entries:
            try:
                reject_links(entry);count+=1
                if count>1000 or entry.is_dir():raise PolicyError('Unexpected guest output tree')
                total+=entry.stat().st_size
            except FileNotFoundError:
                continue  # Guest atomically renames transient reply files.
    return total


def guest_source():
    return Path(__file__).with_name('desktop_guest.py').read_bytes()


def propose(agent,project,task,duration_seconds=600):
    if not available():raise PolicyError('Windows Sandbox unavailable; no host desktop fallback')
    if not isinstance(task,str) or not 1<=len(task)<=2000:raise PolicyError('Describe the desktop task (1–2000 characters)')
    if not 60<=duration_seconds<=1800:raise PolicyError('Session duration must be 60–1800 seconds')
    payload={'project':project,'root':str(agent.project(project)[1]),'task':task,
             'duration_seconds':duration_seconds,'guest_sha256':digest(guest_source()),
             'manifest':agent.snapshot(project),
             'permissions':{'mouse_keyboard':'Sandbox guest only','network':False,'clipboard':False,
                            'host_desktop':False,'host_original_projects':False,'memory_mb':4096}}
    return agent.propose('desktop',payload)


def build_config(input_dir,output,runtime):
    def mapping(host,guest,readonly):
        return f'<MappedFolder><HostFolder>{escape(str(host))}</HostFolder><SandboxFolder>{guest}</SandboxFolder><ReadOnly>{str(readonly).lower()}</ReadOnly></MappedFolder>'
    return ('<Configuration><VGpu>Disable</VGpu><Networking>Disable</Networking>'
            '<AudioInput>Disable</AudioInput><VideoInput>Disable</VideoInput>'
            '<PrinterRedirection>Disable</PrinterRedirection><ClipboardRedirection>Disable</ClipboardRedirection>'
            '<ProtectedClient>Enable</ProtectedClient><MemoryInMB>4096</MemoryInMB><MappedFolders>'
            +mapping(input_dir,r'C:\UDBDesktopInput',True)+mapping(output,r'C:\UDBDesktopOutput',False)
            +mapping(runtime,r'C:\UDBPython',True)
            +r'</MappedFolders><LogonCommand><Command>powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File C:\UDBDesktopInput\start.ps1</Command></LogonCommand></Configuration>')


def session(agent,sid):
    if not re.fullmatch('[a-f0-9]{32}',sid):raise PolicyError('Invalid desktop session ID')
    with agent.connect() as c:row=c.execute('SELECT * FROM jobs WHERE id=?',(sid,)).fetchone()
    if not row:raise PolicyError('Unknown desktop session')
    result=dict(row);result['info']=json.loads(result['info'])
    if result['info'].get('kind')!='desktop':raise PolicyError('This job is not a desktop session')
    return result


def paths(agent,sid):
    # Callers must validate the session ID through session() first.
    folder=agent.state/'desktops'/sid
    reject_links(folder)
    return folder/'input',folder/'output'


def safe_bytes(path,max_bytes):
    """Check the opened handle before reading untrusted guest output (TOCTOU)."""
    reject_links(path)
    with path.open('rb') as f:
        if os.name=='nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel=ctypes.WinDLL('kernel32',use_last_error=True)
            fn=kernel.GetFinalPathNameByHandleW
            fn.argtypes=[wintypes.HANDLE,wintypes.LPWSTR,wintypes.DWORD,wintypes.DWORD];fn.restype=wintypes.DWORD
            name=ctypes.create_unicode_buffer(32768)
            length=fn(msvcrt.get_osfhandle(f.fileno()),name,len(name),0)
            if not length or length>=len(name):raise PolicyError('Cannot verify guest output handle')
            actual=name.value
            if actual.startswith('\\\\?\\'):actual=actual[4:]
        elif Path('/proc/self/fd').is_dir():actual=os.readlink('/proc/self/fd/'+str(f.fileno()))
        else:raise PolicyError('Output handle verification unsupported')
        if os.path.normcase(os.path.normpath(actual))!=os.path.normcase(os.path.abspath(path)):
            raise PolicyError('Guest output redirected outside its expected file')
        if os.fstat(f.fileno()).st_nlink!=1:raise PolicyError('Hardlinked guest output rejected')
        data=f.read(max_bytes+1)
    if len(data)>max_bytes:raise PolicyError('Guest reply exceeds size limit')
    return data


def read_json(path,max_bytes=65536):
    data=safe_bytes(path,max_bytes)
    result=json.loads(data.decode('utf-8-sig'))
    if not isinstance(result,dict):raise PolicyError('Invalid guest reply')
    return result


def status(agent,sid):
    result=session(agent,sid)
    _,output=paths(agent,sid)
    ready=output/'ready.json'
    result['ready']=False
    result['mailbox_protocol']=0
    if ready.is_file():
        proof=read_json(ready)
        if proof.get('session_id')==sid:
            result['ready']=result['status']=='running'
            result['mailbox_protocol']=1 if proof.get('mailbox_protocol')==1 else 0
            result['guest_state_untrusted']=proof.get('state',{})
            result['capabilities']=proof.get('capabilities',[])
    progress=output/'progress.json'
    if progress.is_file():
        try:
            p=read_json(progress)
            if p.get('session_id')==sid:result['progress_untrusted']=p
        except (OSError,ValueError):pass
    result['remaining_seconds']=max(0,int(result['info']['expires']-time.time()))
    return result


def stop(agent,sid):
    value=session(agent,sid)
    if value['status'] not in BUSY:return value
    # The owner monitor holds the actual Popen handle; never kill a persisted PID.
    with agent.connect() as c:c.execute("UPDATE jobs SET status='stopping' WHERE id=? AND status IN ('running','reviewing')",(sid,))
    source,_=paths(agent,sid)
    cancel=source/'cancel-mailbox.json'
    if cancel.is_file():
        reject_links(cancel)
        with cancel.open('r+',encoding='utf8') as f:
            f.seek(0);f.write(canonical({'session_id':sid}));f.truncate();f.flush()
    agent.audit('desktop_stop',{'id':sid})
    return {'session_id':sid,'status':'stopping'}


def validate_action(action,x=0,y=0,text='',keys='',button='left',clicks=1,delta=0,
                    name='',automation_id='',control_type='',value='',timeout_seconds=6,window_title='',
                    steps=None,operation='list',app='',path='',width=0,height=0,max_width=0,window_id=''):
    allowed = ('state','screenshot','move','click','type','press','scroll','open_demo',
               'snapshot','wait_for','element_invoke','element_set_text','batch','app')
    if action not in allowed:raise PolicyError('Unsupported Sandbox desktop action')
    if type(x) is not int or type(y) is not int or not 0<=x<4096 or not 0<=y<2160:
        raise PolicyError('Invalid guest coordinates')
    if button not in ('left','right') or type(clicks) is not int or clicks not in (1,2):
        raise PolicyError('Invalid mouse click')
    if not isinstance(text,str) or len(text)>2000 or '\0' in text:
        raise PolicyError('Text limit: 2000 characters')
    if not isinstance(keys,str) or len(keys)>80:raise PolicyError('Invalid key combination')
    if type(delta) is not int or not -1200<=delta<=1200:raise PolicyError('Scroll limit: +/-1200')
    if type(timeout_seconds) is not int or not 1<=timeout_seconds<=12:
        raise PolicyError('UIA timeout must be 1..12 seconds')
    for field,item,limit in (('name',name,120),('automation_id',automation_id,120),
                             ('control_type',control_type,80),('value',value,2000),('window_title',window_title,160)):
        if not isinstance(item,str) or len(item)>limit or '\0' in item:
            raise PolicyError('Invalid UIA '+field)
    if action in ('wait_for','element_invoke','element_set_text') and not (name or automation_id or control_type):
        raise PolicyError('UIA action requires name, automation_id or control_type')
    if action == 'element_set_text' and not isinstance(value,str):
        raise PolicyError('UIA value must be text')
    if not isinstance(window_id,str) or (window_id and not re.fullmatch('[a-f0-9]{32}',window_id)):
        raise PolicyError('Invalid observed window ID')
    extra={'window_id':window_id}
    if action=='batch':
        try:extra['steps']=validate_steps(steps)
        except ValueError as exc:raise PolicyError(str(exc)) from exc
        for step in extra['steps']:
            validate_action(**step)
    if action=='screenshot':
        if any(type(v) is not int for v in (width,height,max_width)) or not (0<=width<=4096 and 0<=height<=2160 and (max_width==0 or 160<=max_width<=4096)):
            raise PolicyError('Invalid screenshot region or scaling')
        extra.update(width=width,height=height,max_width=max_width)
    if action=='app':
        if operation not in ('list','open','focus','resize','close'):raise PolicyError('Invalid app operation')
        if app not in ('','notepad','explorer') or not isinstance(path,str) or len(path)>200:
            raise PolicyError('Invalid app target')
        if type(width) is not int or type(height) is not int:raise PolicyError('Invalid window size')
        extra.update(operation=operation,app=app,path=path,width=width,height=height)
    return {**extra,'action':action,'x':x,'y':y,'text':text,'keys':keys,'button':button,
            'clicks':clicks,'delta':delta,'name':name,'automation_id':automation_id,
            'control_type':control_type,'value':value,'timeout_seconds':timeout_seconds,
            'window_title':window_title}


def command(agent,sid,action,**arguments):
    started=time.perf_counter()
    payload=validate_action(action,**arguments)
    value=session(agent,sid)
    if value['status']!='running' or time.time()>=value['info']['expires']:raise PolicyError('Desktop session closed or expired')
    if value['info'].get('authorization') and not agent.sandbox_authorization(value['info']['project'],value['info']['root'],value['info']['authorization']):raise PolicyError('Sandbox autonomy revoked')
    guest_status=status(agent,sid)
    if not guest_status['ready']:raise PolicyError('Sandbox desktop is still starting; poll desktop_status')
    mailbox_enabled=(guest_status.get('mailbox_protocol')==1)
    if action in ('batch','app') and 'tasks_v1' not in guest_status.get('capabilities',[]):
        raise PolicyError('Guest worker predates task API; start a new session with the updated build')
    dispatched=time.perf_counter()
    rid=uuid.uuid4().hex;deadline=min(time.time()+45,value['info']['expires'])
    payload.update(session_id=sid,request_id=rid,expires=deadline)
    source,output=paths(agent,sid)
    path=(source/'command-mailbox.json') if mailbox_enabled else (source/'commands'/(rid+'.json'))
    reply=output/'replies'/(rid+'.json')
    with agent.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('CREATE TABLE IF NOT EXISTS desktop_control(session TEXT PRIMARY KEY, request_id TEXT, expires REAL)')
        current=c.execute('SELECT expires FROM desktop_control WHERE session=?',(sid,)).fetchone()
        if current and current[0]>time.time():raise PolicyError('Another desktop action is pending; wait for it')
        c.execute('INSERT OR REPLACE INTO desktop_control VALUES (?,?,?)',(sid,rid,deadline+2))
    try:
        reject_links(path)
        temp=path.with_suffix('.tmp')
        if mailbox_enabled:
            envelope={'payload':payload,'sha256':digest(canonical(payload).encode('utf-8'))}
            # Stable existing mapped file: reader ignores transient incomplete content.
            with path.open('r+',encoding='utf-8',newline='\n') as f:
                f.seek(0);f.write(canonical(envelope));f.truncate();f.flush()
        else:
            with temp.open('x',encoding='utf-8') as f:f.write(canonical(payload))
            os.replace(temp,path)
        published=time.perf_counter();poll_delay=.01;checked=0
        while time.time()<deadline:
            if reply.is_file():
                result=read_json(reply,max_bytes=2*1024*1024)
                if ('request_id' in result or 'session_id' in result) and (result.get('request_id')!=rid or result.get('session_id')!=sid):
                    raise PolicyError('Guest reply identity mismatch')
                if not result.get('ok'):raise PolicyError('Guest action failed: '+str(result.get('error','Unknown'))[:500])
                agent.audit('desktop_action',{'id':sid,'action':action,'request':rid,
                            'text_characters':len(payload.get('text',''))})
                if action=='screenshot':
                    screenshot=output/'replies'/(rid+'.png');png=safe_bytes(screenshot,PNG_LIMIT)
                    validate_png(png);result['_png']=png
                    screenshot.unlink()
                timings=result.setdefault('timings',{})
                timings.update(host_total_ms=round((time.perf_counter()-started)*1000,3),
                               dispatch_ms=round((dispatched-started)*1000,3),
                               publish_ms=round((published-dispatched)*1000,3),
                               wait_verify_audit_ms=round((time.perf_counter()-published)*1000,3))
                return result
            if time.monotonic()-checked>=.1:
                current=session(agent,sid);checked=time.monotonic()
                if current['status']!='running':raise PolicyError('Desktop session ended while waiting')
                info=current['info']
                if info.get('authorization') and not agent.sandbox_authorization(info['project'],info['root'],info['authorization']):
                    raise PolicyError('Sandbox autonomy revoked while waiting')
            time.sleep(poll_delay);poll_delay=min(.04,poll_delay*1.25)
        raise PolicyError('Guest action timed out; no host input fallback')
    finally:
        cleanup=(reply,) if mailbox_enabled else (path,path.with_suffix('.tmp'),reply)
        for item in cleanup:
            reject_links(item);item.unlink(missing_ok=True)
        with agent.connect() as c:c.execute('DELETE FROM desktop_control WHERE session=? AND request_id=?',(sid,rid))


def validate_png(data):
    if len(data)>PNG_LIMIT or not data.startswith(b'\x89PNG\r\n\x1a\n'):raise PolicyError('Invalid or oversized guest PNG')
    position=8;first=True;ended=False;pixels=False
    while position+12<=len(data):
        length=struct.unpack('!I',data[position:position+4])[0]
        kind=data[position+4:position+8];end=position+8+length
        if end+4>len(data):raise PolicyError('Truncated guest PNG')
        body=data[position+8:end]
        if zlib.crc32(kind+body)&0xffffffff!=struct.unpack('!I',data[end:end+4])[0]:raise PolicyError('Guest PNG checksum mismatch')
        if first:
            if kind!=b'IHDR' or len(body)!=13:raise PolicyError('Invalid PNG header')
            width,height=struct.unpack('!II',body[:8])
            if not 1<=width<=4096 or not 1<=height<=2160 or body[8:]!=bytes((8,2,0,0,0)):raise PolicyError('Guest PNG format/dimensions rejected')
            first=False
        elif kind==b'IHDR':raise PolicyError('Duplicate PNG header')
        if kind==b'IDAT':pixels=True
        position=end+4
        if kind==b'IEND':ended=True;break
    if first or not ended or not pixels or position!=len(data):raise PolicyError('Incomplete guest PNG')


def launch(agent,sid,payload):
    from .resources import guard_new_heavy_task
    if not available():raise PolicyError('Windows Sandbox unavailable; no host fallback')
    worker=guest_source()
    if digest(worker)!=payload['guest_sha256']:raise PolicyError('Guest controller changed; request approval again')
    cfg=agent.state/'runtime.json'
    runtime=Path(json.loads(cfg.read_text('utf-8-sig'))['python_runtime']) if cfg.exists() else (Path(sys.executable).parent/'vendor'/'python-runtime' if getattr(sys,'frozen',False) else Path(sys.base_prefix))
    reject_links(runtime)
    if not (runtime/'python.exe').is_file() or runtime==agent.home or agent.state.is_relative_to(runtime):raise PolicyError('Trusted standalone Python runtime required')
    guard_new_heavy_task(agent, 4096)
    folder=agent.state/'desktops'/sid;reject_links(folder)
    source,output=folder/'input',folder/'output'
    source.mkdir(parents=True,exist_ok=False);output.mkdir();(source/'commands').mkdir();(source/'source').mkdir()
    (source/'command-mailbox.json').write_text('{}',encoding='utf-8')
    (source/'cancel-mailbox.json').write_text('{}',encoding='utf-8')
    for rel,sha in payload['manifest'].items():
        data=agent.read_bytes(payload['project'],rel)
        if digest(data)!=sha:raise PolicyError('Project changed since approval')
        dest=source/'source'/rel;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
    expires=time.time()+payload['duration_seconds']+90
    config={'session_id':sid,'guest_only':True,'host_name':socket.gethostname(),'expires':expires,'task':payload['task']}
    (source/'session.json').write_text(canonical(config),encoding='utf-8')
    (source/'guest.py').write_bytes(worker)
    (source/'uia.ps1').write_bytes(Path(__file__).with_name('desktop_uia.ps1').read_bytes())
    (source/'accessibility.ps1').write_bytes(Path(__file__).with_name('desktop_accessibility.ps1').read_bytes())
    (source/'overlay.ps1').write_bytes(Path(__file__).with_name('desktop_overlay.ps1').read_bytes())
    (source/'demo.ps1').write_text(DEMO,encoding='utf-8-sig')
    (source/'start.ps1').write_text(r'''$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Path C:\Work -Force | Out-Null
Copy-Item C:\UDBDesktopInput\source\* C:\Work -Recurse -Force -ErrorAction SilentlyContinue
Set-Location C:\Work
& C:\UDBPython\python.exe -I -B -u C:\UDBDesktopInput\guest.py *> C:\UDBDesktopOutput\worker.log
''',encoding='utf-8-sig')
    profile=folder/'desktop.wsb';profile.write_text(build_config(source,output,runtime),encoding='utf-8')
    info={'kind':'desktop','project':payload['project'],'task':payload['task'],'started':time.time(),
          'expires':expires,'permissions':payload['permissions'],'source_files':len(payload['manifest']),
          'guest_sha256':payload['guest_sha256'],'guest_output_untrusted':True,
          'root':payload['root'],'authorization':payload.get('authorization')}
    reserve_vm(agent,sid,info)
    try:proc=subprocess.Popen([str(executable()),str(profile)],shell=False)
    except Exception:
        with agent.connect() as c:c.execute("UPDATE jobs SET status='failed' WHERE id=?",(sid,))
        raise
    with _OWNED_LOCK:_OWNED[sid]=(agent,proc)
    def monitor():
        final='completed';error=None
        startup_deadline=time.time()+90
        window_shown=False
        try:
            while time.time()<expires:
                if not window_shown and (output/'ready.json').is_file():window_shown=restore_owned_vm(proc)
                current=session(agent,sid)
                if current['status'] in ('stopping','stopped') or (info.get('authorization') and not agent.sandbox_authorization(info['project'],info['root'],info['authorization'])):final='stopped';break
                if proc.poll() is not None:
                    if not (output/'ready.json').is_file():final='failed';error='Guest did not start; inspect worker.log and Windows Sandbox'
                    break
                if time.time()>startup_deadline and not (output/'ready.json').is_file():
                    final='failed';error='Guest controller did not become ready within 90 seconds';break
                if output_bytes(output)>32*1024*1024:final='output_limit';break
                time.sleep(.5)
            else:final='timeout'
        except Exception as exc:final='failed';error=str(exc)
        finally:
            if proc.poll() is None:proc.terminate()
            if error:info['error']=error
            info['finished']=time.time()
            with agent.connect() as c:c.execute('UPDATE jobs SET status=?,info=? WHERE id=?',(final,canonical(info),sid))
            with _OWNED_LOCK:_OWNED.pop(sid,None)
            agent.audit('desktop_finish',{'id':sid,'status':final})
    threading.Thread(target=monitor,daemon=True).start()
    agent.audit('desktop_start',{'id':sid,'project':payload['project'],'guest_sha256':payload['guest_sha256']})
    return {'session_id':sid,'status':'running','next':'Poll desktop_status; screenshots and input stay inside the guest VM.'}
