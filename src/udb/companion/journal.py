"""Bounded private work records. Never execute anything while loading records."""
import hashlib
import json
import os
from pathlib import Path
import re
import threading
import time

MAX_RECORD=2*1024*1024

def protect(data,decrypt=False):
    if os.name!='nt':return data
    import ctypes
    from ctypes import wintypes as w
    class Blob(ctypes.Structure):_fields_=[('size',w.DWORD),('data',ctypes.c_void_p)]
    crypt=ctypes.WinDLL('crypt32',use_last_error=True);kernel=ctypes.WinDLL('kernel32')
    fn=crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    fn.argtypes=[ctypes.POINTER(Blob),ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,ctypes.c_void_p,w.DWORD,ctypes.POINTER(Blob)];fn.restype=w.BOOL
    kernel.LocalFree.argtypes=[ctypes.c_void_p]
    buffer=ctypes.create_string_buffer(data);source=Blob(len(data),ctypes.addressof(buffer));out=Blob()
    if not fn(ctypes.byref(source),None,None,None,None,1,ctypes.byref(out)):raise ctypes.WinError(ctypes.get_last_error())
    try:return ctypes.string_at(out.data,out.size)
    finally:kernel.LocalFree(out.data)

class Journal:
    def __init__(self,root):
        self.path=Path(root)/'.udb-work-state';self.lock=threading.RLock();self.hashes={}
        self.check(self.path,missing=True)
    def check(self,path,missing=False):
        for p in (path,*path.parents):
            if p.is_symlink() or (p.exists() and getattr(p.lstat(),'st_file_attributes',0)&0x400):raise ValueError('Redirected work state refused')
        if path.exists() and path.is_file() and path.stat().st_nlink!=1:raise ValueError('Linked work state refused')
    def file(self,key):
        if not re.fullmatch(r'(job|terminal)-[a-f0-9]{12,32}',key):raise ValueError('Invalid work ID')
        p=self.path/(key+'.json');self.check(p,True);return p
    def save(self,key,row):
        data=json.dumps(row,ensure_ascii=True,allow_nan=False,separators=(',',':')).encode()
        if len(data)>MAX_RECORD:raise ValueError('Work record limit exceeded')
        digest=hashlib.sha256(data).hexdigest()
        with self.lock:
            if self.hashes.get(key)==digest:return
            self.check(self.path,True);self.path.mkdir(mode=0o700,exist_ok=True)
            p=self.file(key);new_record=not p.exists();temp=p.with_suffix('.'+os.urandom(6).hex()+'.tmp')
            stored=b'UDBW1'+protect(data) if os.name=='nt' else data
            try:
                fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as f:f.write(stored);f.flush();os.fsync(f.fileno())
                self.check(p,True);os.replace(temp,p);self.hashes[key]=digest
            finally:temp.unlink(missing_ok=True)
            if new_record:self.prune(key.split('-')[0])
    def prune(self,prefix):
        candidates=sorted(self.path.glob(prefix+'-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for p in candidates[64:]:
            try:lease=self.lease(p.stem)
            except RuntimeError:continue
            try:self.check(p);p.unlink(missing_ok=True);self.hashes.pop(p.stem,None)
            finally:lease.close()
    def load(self,key):
        p=self.file(key)
        with p.open('rb') as f:data=f.read(MAX_RECORD+4097)
        if len(data)>MAX_RECORD+4096:raise ValueError('Oversized work record')
        if os.name=='nt':
            if not data.startswith(b'UDBW1'):raise ValueError('Unencrypted work record refused')
            data=protect(data[5:],True)
        value=json.loads(data)
        if not isinstance(value,dict):raise ValueError('Invalid work record')
        return value
    def records(self,prefix):
        rows=[]
        files=sorted(self.path.glob(prefix+'-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        for p in files[:64]:
            try:rows.append(self.load(p.stem))
            except (OSError,ValueError):continue
        return rows
    def lease(self,key):
        """OS advisory byte lock, released even after a process crash."""
        self.check(self.path,True);self.path.mkdir(mode=0o700,exist_ok=True)
        self.file(key);p=self.path/(key+'.lock');self.check(p,True)
        fd=os.open(p,os.O_RDWR|os.O_CREAT,0o600);f=os.fdopen(fd,'r+b',buffering=0)
        try:
            if os.fstat(fd).st_size==0:f.write(b'0')
            f.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(fd,msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return f
        except BaseException:f.close();raise RuntimeError('Work is owned by another live backend')

class WorkRecovery:
    def __init__(self,rt):self.rt=rt;self.bg=rt.features.background if hasattr(rt,'features') else None
    def call(self,action='list',kind='job',work_id=''):
        bg=self.rt.features.background;journal=bg.journal
        if kind not in ('job','terminal'):raise ValueError('kind job or terminal')
        if action=='list':
            rows=journal.records(kind)
            return dict(records=[{k:v for k,v in row.items() if k in ('job_id','session_id','kind','state','exit_code','terminal_mode','committed','resumed_as')} for row in rows],automatic_replay=False)
        key=kind+'-'+work_id
        row=journal.load(key)
        if action=='inspect':
            if kind=='job' and work_id in bg.tasks:return bg.tasks[work_id].read()
            if kind=='terminal' and work_id in self.rt.sessions:return self.rt.sessions[work_id].read(None,0)
            if row['state']=='running':
                try:lease=journal.lease(key)
                except RuntimeError:row['state']='owned_by_live_backend'
                else:lease.close();row['state']='interrupted';row['outcome_unknown']=not row.get('committed',False)
            if isinstance(row.get('results'),list):
                row['results_total']=len(row['results']);row['results']=row['results'][:100];row['results_truncated']=row['results_total']>100
            row['automatic_replay']=False
            return row
        if action!='resume':raise ValueError('action list, inspect or resume')
        if kind=='terminal':raise ValueError('A dead process cannot be restored. Read its saved transcript; no command replay is performed')
        if work_id in bg.tasks and bg.tasks[work_id].state=='running':raise ValueError('Work is still running')
        lease=journal.lease(key)
        try:
            row=journal.load(key)
            if row.get('resumed_as'):return dict(job_id=row['resumed_as'],already_resumed=True)
            if row['state']=='completed' or row.get('committed'):return dict(job_id=work_id,state=row['state'],already_finished=True,result=row.get('result',{}))
            recovery=row.get('recovery')
            if not isinstance(recovery,dict):raise ValueError('Outcome may be unknown; this operation is not resumable')
            name=recovery.get('tool');args=recovery.get('arguments',{})
            if not isinstance(args,dict):raise ValueError('Invalid recovery arguments')
            if name=='search_start':new=bg.search(**args)
            elif name=='file_operation':
                if args.get('action')!='copy' or set(args)-{'action','source','destination'}:raise ValueError('Only guarded create-only file copy can resume')
                from udb.companion.background import version
                source=self.rt.files.path(args['source'])
                if not source.is_file() or version(source)!=recovery.get('source_version'):raise ValueError('Source changed since interruption')
                new=bg.file_operation(**args,resume_version=recovery['source_version'])
            elif name in ('spreadsheet','word_document','pdf_document') and args.get('action') in ('read','inspect'):
                new=self.rt.features.documents.start(name,**args)
            else:raise ValueError('This work cannot safely resume')
            row['resumed_as']=new['job_id'];journal.save(key,row)
            return dict(new,recovered_from=work_id,recovery_mode='restart_verified_operation',automatic_replay=False)
        finally:lease.close()
