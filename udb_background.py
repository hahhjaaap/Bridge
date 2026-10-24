"""Bounded background search and file jobs. All paths go through ScopedFiles."""
from __future__ import annotations
import base64
import ctypes
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import threading
import time
import uuid
sys.path.insert(0,str(Path(__file__).resolve().parent))
from udb_managed_process import ManagedProcess

MAX_LARGE = 16*1024**3
CHUNK = 1024*1024


def identity(p):
    s=p.stat();return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)


def version(p):return hashlib.sha256(repr(identity(p)).encode()).hexdigest()


def publish_new(source,destination):
    """Atomic rename, never replace a concurrently created destination."""
    if os.name=='nt':
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.MoveFileExW.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32];k.MoveFileExW.restype=ctypes.c_int
        if not k.MoveFileExW(str(source),str(destination),0):raise ctypes.WinError(ctypes.get_last_error())
    elif sys.platform.startswith('linux'):
        libc=ctypes.CDLL(None,use_errno=True)
        rename=libc.renameat2;rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint];rename.restype=ctypes.c_int
        if rename(-100,os.fsencode(source),-100,os.fsencode(destination),1):raise OSError(ctypes.get_errno(),'Atomic no-replace rename failed')
    else:raise RuntimeError('Atomic no-replace publish requires Windows or Linux')


class Cancelled(Exception):pass


class Task:
    def __init__(self,kind):
        self.id=uuid.uuid4().hex;self.kind=kind;self.state='running';self.error='';self.result={}
        self.cancel=threading.Event();self.lock=threading.RLock();self.rows=[];self.progress={};self.worker=None;self.thread=None;self.committed=False;self.deadline=time.monotonic()+3600
    def check(self):
        if self.cancel.is_set():raise Cancelled()
        if time.monotonic()>self.deadline:raise TimeoutError("Background job exceeded 3600 seconds")
    def commit(self,fn):
        with self.lock:
            self.check();fn();self.committed=True
    def start(self,fn):
        def run():
            try:
                self.result=fn(self)
                self.state='completed'
            except Cancelled:self.state='cancelled'
            except Exception as exc:self.state='error';self.error=type(exc).__name__+': '+str(exc)
        self.thread=threading.Thread(target=run,daemon=True,name='UDB '+self.kind);self.thread.start();return self.id
    def read(self,offset=0,limit=50):
        if not 0<=int(offset)<=2000 or not 1<=int(limit)<=100:raise ValueError('Job offset/limit out of range')
        with self.lock:
            end=min(len(self.rows),int(offset)+int(limit))
            return dict(job_id=self.id,kind=self.kind,state=self.state,error=self.error,result=self.result,
                progress=dict(self.progress),results=self.rows[int(offset):end],next_offset=end,
                has_more=end<len(self.rows),committed=self.committed,cancellation_requested=self.cancel.is_set())
    def stop(self):
        with self.lock:
            if not self.committed:self.cancel.set()
            if self.worker:self.worker.terminate()
        return self.read()


class Background:
    def __init__(self,files):self.files=files;self.tasks={};self.uploads={};self.upload_history={}
    def new(self,kind,fn,before_start=None):
        if sum(t.state=='running' for t in self.tasks.values())>=2:raise RuntimeError('Maximum two background jobs')
        while len(self.tasks)>=32:
            old=next((k for k,t in self.tasks.items() if t.state!='running'),None)
            if old is None:break
            del self.tasks[old]
        task=Task(kind);self.tasks[task.id]=task
        if before_start:before_start(task)
        try:task.start(fn)
        except Exception:self.tasks.pop(task.id,None);raise
        return task.read()
    def task(self,jid,action='read',offset=0,limit=50):
        if action=='list':return {'jobs':[dict(job_id=t.id,kind=t.kind,state=t.state) for t in self.tasks.values()]}
        if jid not in self.tasks:raise ValueError('Unknown job; background jobs do not survive restart')
        return self.tasks[jid].stop() if action=='cancel' else self.tasks[jid].read(offset,limit)
    def close(self):
        for t in list(self.tasks.values()):
            if t.state=='running':t.stop()
        for t in list(self.tasks.values()):
            if t.thread and t.thread is not threading.current_thread():t.thread.join(timeout=3)
        for key in list(self.uploads):self.upload('abort',upload_id=key)

    def expire(self):
        for key,row in list(self.uploads.items()):
            if row.get('state')!='committing' and time.monotonic()-row['at']>900:
                self.upload('abort',upload_id=key)

    def search(self,**args):
        root=self.files.path(args.get('path','.'))
        if not root.is_dir():raise ValueError('Search path must be a directory')
        query=args.get('text','');pattern=args.get('pattern','*')
        if not isinstance(query,str) or len(query)>500 or not isinstance(pattern,str) or len(pattern)>200:raise ValueError('Bounded search text/pattern required')
        seconds=int(args.get('timeout_seconds',30))
        if not 1<=seconds<=120:raise ValueError('Search timeout 1..120 seconds')
        config=dict(root=str(self.files.root),path=str(root),text=query,pattern=pattern,
            regex=bool(args.get('regex',False)),case_sensitive=bool(args.get('case_sensitive',False)),search_type=args.get('search_type','content'))
        if config['search_type'] not in ('files','content'):raise ValueError('Search type files or content')
        def run(task):
            proc=ManagedProcess([sys.executable,'-u',str(Path(__file__).resolve()),'--search-worker'],str(self.files.root),256)
            task.worker=proc;deadline=time.monotonic()+seconds
            def timeout():
                while proc.poll() is None:
                    if task.cancel.wait(.05) or time.monotonic()>deadline:
                        proc.terminate();return
            timer=threading.Thread(target=timeout,daemon=True);timer.start()
            try:
                proc.stdin.write((json.dumps(config)+'\n').encode());proc.stdin.close()
                result={}
                for line in iter(lambda:proc.stdout.readline(65537),b''):
                    task.check()
                    if len(line)>65536:raise ValueError('Search output frame exceeded limit')
                    row=json.loads(line)
                    with task.lock:
                        if row['type']=='match':
                            if len(task.rows)>=2000:raise ValueError('Search result cap exceeded')
                            task.rows.append(row['value'])
                        elif row['type']=='progress':task.progress=row['value']
                        elif row['type']=='done':result=row['value']
                        elif row['type']=='error':raise ValueError(row['value'])
                task.check()
                if time.monotonic()>deadline:raise TimeoutError('Regex/search budget exhausted; partial results retained')
                if proc.wait(timeout=3)!=0 or not result:raise RuntimeError('Search worker failed; partial results retained')
                return result
            finally:
                proc.close();task.worker=None
                for stream in (proc.stdin,proc.stdout):
                    try:stream.close()
                    except (OSError,ValueError):pass
        return self.new('search',run)

    def read_chunk(self,path,offset=0,limit=65536,expected_version=''):
        p=self.files.path(path);start=int(offset);count=int(limit)
        if start<0 or not 1<=count<=CHUNK:raise ValueError('Chunk limit 1..1048576; offset must be nonnegative')
        if not p.is_file() or p.stat().st_size>MAX_LARGE:raise ValueError('Regular file up to 16 GiB required')
        before=version(p)
        if expected_version and expected_version!=before:raise ValueError('File changed since previous chunk')
        with p.open('rb') as stream:
            opened=os.fstat(stream.fileno())
            if opened.st_nlink>1 or (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)!=identity(p):raise ValueError('File identity changed')
            stream.seek(start);data=stream.read(count)
        if version(p)!=before:raise ValueError('File changed while reading')
        return dict(path=str(p.relative_to(self.files.root)),offset=start,next_offset=start+len(data),size=opened.st_size,
            eof=start+len(data)>=opened.st_size,version=before,encoding='base64',content=base64.b64encode(data).decode(),chunk_sha256=hashlib.sha256(data).hexdigest())

    def upload(self,action,path='',upload_id='',content='',offset=0,size=0,sha256=''):
        if action=='begin':
            if len(self.uploads)>=2:raise RuntimeError('Maximum two open uploads')
            dst=self.files.path(path,allow_missing=True)
            if dst.exists() or dst.is_symlink():raise FileExistsError('Upload is create-only; destination exists')
            self.files.path(str(dst.parent))
            if not 0<=int(size)<=MAX_LARGE or not re.fullmatch('[0-9a-fA-F]{64}',sha256):raise ValueError('Expected size (0..16 GiB) and SHA256 required')
            fd,temp=tempfile.mkstemp(prefix='.udb-upload-',dir=str(dst.parent));os.close(fd)
            uid=uuid.uuid4().hex;self.uploads[uid]=dict(path=dst,temp=Path(temp),identity=identity(Path(temp)),size=int(size),sha256=sha256.lower(),offset=0,hasher=hashlib.sha256(),at=time.monotonic())
            return dict(upload_id=uid,offset=0,size=int(size),state='open')
        if upload_id not in self.uploads:
            if action=='status' and upload_id in self.upload_history:return self.upload_history[upload_id]
            if upload_id in self.upload_history:raise ValueError('Upload already finalized; inspect status instead of replaying')
            raise ValueError('Unknown upload; uploads do not survive restart')
        row=self.uploads[upload_id];temp=row['temp'];row['at']=time.monotonic()
        if action=='status':return dict(upload_id=upload_id,offset=row['offset'],size=row['size'],state=row.get('state','open'),job_id=row.get('job_id'))
        if action=='abort':
            if row.get('state')=='committing':return self.task(row['job_id'],'cancel')
            self.uploads.pop(upload_id)
            self.upload_history[upload_id]=dict(upload_id=upload_id,state='aborted')
            if len(self.upload_history)>64:self.upload_history.pop(next(iter(self.upload_history)))
            if temp.exists() and identity(temp)[:2]==row['identity'][:2]:temp.unlink()
            return dict(upload_id=upload_id,state='aborted')
        if row.get('state')=='committing':raise ValueError('Upload commit already running; inspect its job instead of replaying')
        if identity(temp)[:2]!=row['identity'][:2] or temp.stat().st_nlink!=1 or temp.stat().st_size!=row['offset']:raise ValueError('Upload staging identity/size changed')
        if action=='write':
            if len(content)>((CHUNK+2)//3)*4:raise ValueError('Chunk exceeds 1 MiB')
            data=base64.b64decode(content,validate=True)
            if int(offset)!=row['offset'] or len(data)>CHUNK or row['offset']+len(data)>row['size']:raise ValueError('Expected next offset and bounded upload data')
            with temp.open('ab') as f:f.write(data)
            row['hasher'].update(data);row['offset']+=len(data)
            return dict(upload_id=upload_id,next_offset=row['offset'],size=row['size'])
        if action!='commit':raise ValueError('Upload actions: begin/write/status/commit/abort')
        if row['offset']!=row['size'] or row['hasher'].hexdigest()!=row['sha256']:raise ValueError('Upload size/SHA256 mismatch; nothing published')
        def commit(task):
            try:
                actual=hashlib.sha256()
                with temp.open('rb') as stream:
                    for data in iter(lambda:stream.read(CHUNK),b''):task.check();actual.update(data)
                if actual.hexdigest()!=row['sha256'] or temp.stat().st_size!=row['size']:raise ValueError('Staged bytes failed final SHA256/size verification')
                dst=self.files.path(str(row['path']),allow_missing=True)
                with temp.open('ab') as stream:stream.flush();os.fsync(stream.fileno())
                task.commit(lambda:publish_new(temp,dst))
                return dict(path=str(dst.relative_to(self.files.root)),size=row['size'],sha256=row['sha256'],replay_safe=False)
            finally:
                self.upload_history[upload_id]=dict(upload_id=upload_id,job_id=task.id,state='completed' if task.committed else 'cancelled' if task.cancel.is_set() else 'error',path=str(row['path'].relative_to(self.files.root)),sha256=row['sha256'] if task.committed else None)
                if len(self.upload_history)>64:self.upload_history.pop(next(iter(self.upload_history)))
                self.uploads.pop(upload_id,None)
                if temp.exists() and identity(temp)[:2]==row['identity'][:2]:temp.unlink()
        row['state']='committing'
        try:
            return self.new('upload_commit',commit,before_start=lambda task:row.update(job_id=task.id))
        except Exception:
            row.pop('state',None);raise

    def file_operation(self,action,source='',destination='',job_id='',**kwargs):
        if action in ('read','list','cancel'):return self.task(job_id,action,kwargs.get('offset',0),kwargs.get('limit',50))
        if action not in ('copy','move'):raise ValueError('File operation actions: copy/move/read/list/cancel')
        src=self.files.path(source);dst=self.files.path(destination,allow_missing=True)
        if src==self.files.root or src==dst or dst.is_relative_to(src):raise ValueError('Cannot move/copy project root or into its source')
        if dst.exists() or dst.is_symlink():raise FileExistsError('Destination exists; overwrite disabled')
        self.files.path(str(dst.parent))
        def run(task):
            entries=[];total=0
            def visit(p):
                nonlocal total
                task.check();p=self.files.path(str(p));meta=identity(p)
                entries.append((p,p.relative_to(src) if p!=src else Path('.'),p.is_dir(),meta))
                if len(entries)>20000:raise ValueError('Tree exceeds 20,000 entries')
                if p.is_file():
                    total+=meta[2]
                    if total>MAX_LARGE:raise ValueError('Tree exceeds 16 GiB total limit')
                elif p.is_dir():
                    for child in p.iterdir():visit(child)
                else:raise ValueError('Only regular files and directories can be copied')
            visit(src)
            task.progress=dict(entries=len(entries),total_bytes=total,copied_bytes=0)
            def verify():
                for p,rel,isdir,meta in entries:
                    task.check();self.files.path(str(p))
                    if identity(p)!=meta:raise ValueError('Source tree changed; operation cancelled')
                self.files.path(str(dst.parent))
            if action=='move':
                verify();task.commit(lambda:publish_new(src,dst))
                return dict(source=str(src.relative_to(self.files.root)),destination=str(dst.relative_to(self.files.root)),entries=len(entries),bytes=total)
            stage=Path(tempfile.mkdtemp(prefix='.udb-stage-',dir=str(dst.parent)))
            try:
                target=stage/'payload'
                for p,rel,isdir,meta in entries:
                    task.check();self.files.path(str(p));out=target/rel if rel!=Path('.') else target
                    if isdir:out.mkdir()
                    else:
                        h=hashlib.sha256()
                        with p.open('rb') as incoming,out.open('xb') as outgoing:
                            while True:
                                task.check();data=incoming.read(CHUNK)
                                if not data:break
                                outgoing.write(data);h.update(data)
                                with task.lock:task.progress['copied_bytes']+=len(data)
                            outgoing.flush();os.fsync(outgoing.fileno())
                        verify_hash=hashlib.sha256()
                        with out.open('rb') as copied:
                            for data in iter(lambda:copied.read(CHUNK),b''):task.check();verify_hash.update(data)
                        if verify_hash.digest()!=h.digest():raise IOError('Destination checksum mismatch')
                verify();task.commit(lambda:publish_new(target,dst))
                return dict(destination=str(dst.relative_to(self.files.root)),entries=len(entries),bytes=total,sha256_verified=True)
            finally:shutil.rmtree(stage,ignore_errors=True)
        return self.new('file_'+action,run)


def search_worker():
    from udb_companion import ScopedFiles
    config=json.loads(sys.stdin.buffer.readline(65536));files=ScopedFiles(config['root']);root=files.path(config['path'])
    query=config['text'];flags=0 if config['case_sensitive'] else re.IGNORECASE
    rx=None
    scanned=matches=0;truncated=False
    def send(kind,value):print(json.dumps(dict(type=kind,value=value),ensure_ascii=True),flush=True)
    try:
        rx=re.compile(query,flags) if config['regex'] else None
        for folder,dirs,names in os.walk(root,followlinks=False):
            def allowed(p):
                try:files.path(str(p));return True
                except (OSError,ValueError):return False
            dirs[:]=[n for n in dirs if allowed(Path(folder)/n)]
            for name in names:
                scanned+=1
                if scanned>20000:truncated=True;break
                if scanned%100==0:send('progress',dict(files_scanned=scanned,matches=matches))
                if not fnmatch.fnmatch(name,config['pattern']):continue
                p=Path(folder)/name
                if not allowed(p) or not p.is_file():continue
                if config['search_type']=='files':
                    found=not query or (bool(rx.search(name)) if rx else (query in name if config['case_sensitive'] else query.casefold() in name.casefold()))
                    values=[dict(path=str(p.relative_to(files.root)))] if found else []
                else:
                    if p.stat().st_size>64*1024*1024:continue
                    values=[]
                    try:
                        with p.open('r',encoding='utf-8',errors='strict') as f:
                            lineno=0
                            while True:
                                line=f.readline(8193)
                                if not line:break
                                lineno+=1
                                if len(line)>8192 and not line.endswith('\n'):
                                    while line and not line.endswith('\n'):line=f.readline(8193)
                                    continue
                                found=bool(rx.search(line)) if rx else (query in line if config['case_sensitive'] else query.casefold() in line.casefold())
                                if found:
                                    send('match',dict(path=str(p.relative_to(files.root)),line=lineno,text=line.rstrip()[:500]));matches+=1
                                    if matches>=2000:break
                    except (OSError,UnicodeError):continue
                for value in values:send('match',value);matches+=1
                if matches>=2000:truncated=True;break
            if truncated:break
        send('done',dict(files_scanned=scanned,matches=matches,truncated=truncated))
    except Exception as exc:send('error',type(exc).__name__+': '+str(exc));raise SystemExit(1)

if __name__=='__main__':
    if sys.argv[1:]==['--search-worker']:search_worker()
