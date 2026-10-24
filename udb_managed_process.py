"""Owned process trees. Windows 10+: assign the Job atomically at creation.

No elevation, external dependencies, shell parsing or inherited auth variables.
POSIX is a development fallback; production Windows uses kernel kill-on-close.
"""
from __future__ import annotations
import ctypes
import os
import signal
import subprocess
import threading
import time

_LOCK = threading.RLock()


def clean_env():
    env = os.environ.copy()
    for key in ('CONTROL_PLANE_API_KEY', 'TUNNEL_CLIENT_PROFILE_DIR'):
        env.pop(key, None)
    env['PYTHONIOENCODING'] = 'utf-8'
    env['PYTHONUTF8'] = '1'
    return env


class ManagedProcess:
    def __init__(self, argv, cwd, memory_mib=1024):
        self.lock = threading.RLock()
        self.returncode = None
        self.job = self.handle = None
        self.stdin = self.stdout = None
        if os.name != 'nt':
            self.proc = subprocess.Popen(argv, cwd=cwd, env=clean_env(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0, start_new_session=True)
            self.pid = self.proc.pid; self.stdin = self.proc.stdin; self.stdout = self.proc.stdout
            return
        from ctypes import wintypes as w
        import msvcrt
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        self.k = k
        HANDLE = w.HANDLE
        def api(name, restype, *types):
            f = getattr(k, name); f.restype = restype; f.argtypes = list(types); return f
        api('CreateJobObjectW', HANDLE, ctypes.c_void_p, w.LPCWSTR)
        api('SetInformationJobObject', w.BOOL, HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD)
        api('TerminateJobObject', w.BOOL, HANDLE, w.UINT)
        api('CloseHandle', w.BOOL, HANDLE)
        api('GetExitCodeProcess', w.BOOL, HANDLE, ctypes.POINTER(w.DWORD))
        api('WaitForSingleObject', w.DWORD, HANDLE, w.DWORD)
        api('ResumeThread', w.DWORD, HANDLE)
        api('InitializeProcThreadAttributeList', w.BOOL, ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_size_t))
        api('UpdateProcThreadAttribute', w.BOOL, ctypes.c_void_p, w.DWORD, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p)
        api('DeleteProcThreadAttributeList', None, ctypes.c_void_p)
        class Basic(ctypes.Structure):
            _fields_ = [('ProcessTime', ctypes.c_int64), ('JobTime', ctypes.c_int64),
                ('Flags', w.DWORD), ('MinWS', ctypes.c_size_t), ('MaxWS', ctypes.c_size_t),
                ('Active', w.DWORD), ('Affinity', ctypes.c_size_t), ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(x, ctypes.c_uint64) for x in ('r','w','o','rb','wb','ob')]
        class Limits(ctypes.Structure):
            _fields_ = [('Basic', Basic), ('IO', IO), ('ProcessMemory', ctypes.c_size_t),
                ('JobMemory', ctypes.c_size_t), ('PeakProcess', ctypes.c_size_t), ('PeakJob', ctypes.c_size_t)]
        class Startup(ctypes.Structure):
            _fields_ = [('cb',w.DWORD),('reserved',w.LPWSTR),('desktop',w.LPWSTR),('title',w.LPWSTR)] + [
                (x,w.DWORD) for x in ('x','y','xs','ys','charsx','charsy','fill','flags')] + [
                ('show',w.WORD),('reserved2',w.WORD),('bytes',ctypes.c_void_p),
                ('stdin',HANDLE),('stdout',HANDLE),('stderr',HANDLE)]
        class StartupEx(ctypes.Structure):
            _fields_ = [('si',Startup),('attributes',ctypes.c_void_p)]
        class PI(ctypes.Structure):
            _fields_ = [('process',HANDLE),('thread',HANDLE),('pid',w.DWORD),('tid',w.DWORD)]
        api('CreateProcessW',w.BOOL,w.LPCWSTR,w.LPWSTR,ctypes.c_void_p,ctypes.c_void_p,w.BOOL,w.DWORD,ctypes.c_void_p,w.LPCWSTR,ctypes.POINTER(StartupEx),ctypes.POINTER(PI))
        self.job = k.CreateJobObjectW(None, None)
        if not self.job: raise ctypes.WinError(ctypes.get_last_error())
        fds = []; attrs = None; pi = PI()
        try:
            limits = Limits(); limits.Basic.Flags = 0x2000 | 0x8 | 0x200
            limits.Basic.Active = 32; limits.JobMemory = int(memory_mib)*1048576
            if not k.SetInformationJobObject(self.job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            rin, win = os.pipe(); fds += [rin,win]
            rout, wout = os.pipe(); fds += [rout,wout]
            child = (HANDLE*2)(msvcrt.get_osfhandle(rin), msvcrt.get_osfhandle(wout))
            jobs = (HANDLE*1)(self.job)
            size = ctypes.c_size_t()
            k.InitializeProcThreadAttributeList(None,2,0,ctypes.byref(size))
            attrs = ctypes.create_string_buffer(size.value)
            if not k.InitializeProcThreadAttributeList(attrs,2,0,ctypes.byref(size)):
                attrs = None; raise ctypes.WinError(ctypes.get_last_error())
            # HANDLE_LIST and JOB_LIST: no child can run or escape between
            # process creation and job assignment, even if this parent crashes.
            for key, value in [(0x20002,child),(0x2000D,jobs)]:
                if not k.UpdateProcThreadAttribute(attrs,0,key,value,ctypes.sizeof(value),None,None):
                    raise ctypes.WinError(ctypes.get_last_error())
            si = StartupEx(); si.si.cb = ctypes.sizeof(si); si.attributes = ctypes.addressof(attrs)
            si.si.flags = 0x100; si.si.stdin = child[0]; si.si.stdout = si.si.stderr = child[1]
            env = clean_env(); block = ctypes.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(env.items(),key=lambda x:x[0].upper()))+'\0\0')
            line = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
            with _LOCK:
                os.set_inheritable(rin,True); os.set_inheritable(wout,True)
                try:
                    ok = k.CreateProcessW(argv[0],line,None,None,True,0x80000|0x4|0x08000000|0x400,block,str(cwd),ctypes.byref(si),ctypes.byref(pi))
                    if not ok: raise ctypes.WinError(ctypes.get_last_error())
                    self.handle = pi.process; self.pid = int(pi.pid)
                    if k.ResumeThread(pi.thread)==0xFFFFFFFF:raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    os.set_inheritable(rin,False); os.set_inheritable(wout,False)
            os.close(rin); fds.remove(rin); os.close(wout); fds.remove(wout)
            self.stdin = os.fdopen(win,'wb',buffering=0); fds.remove(win)
            self.stdout = os.fdopen(rout,'rb',buffering=0); fds.remove(rout)
        except BaseException:
            self.close(); raise
        finally:
            if pi.thread:k.CloseHandle(pi.thread)
            if attrs is not None:k.DeleteProcThreadAttributeList(attrs)
            for fd in fds:os.close(fd)

    def poll(self):
        with self.lock:
            if self.returncode is not None:return self.returncode
            if os.name!='nt':
                code=self.proc.poll()
                if code is not None:
                    self.terminate();self.returncode=code
                return code
            from ctypes import wintypes as w
            if not self.handle:return self.returncode
            if self.k.WaitForSingleObject(self.handle,0)==0:
                code=w.DWORD()
                if not self.k.GetExitCodeProcess(self.handle,ctypes.byref(code)):raise ctypes.WinError(ctypes.get_last_error())
                self.returncode=int(code.value)
                # A terminal root finishing must not leave background children.
                if self.job:self.k.TerminateJobObject(self.job,1);self.k.CloseHandle(self.job);self.job=None
                self.k.CloseHandle(self.handle);self.handle=None
            return self.returncode

    def terminate(self):
        with self.lock:
            if self.returncode is not None:return
            if os.name=='nt':
                if self.job and not self.k.TerminateJobObject(self.job,1):raise ctypes.WinError(ctypes.get_last_error())
            else:
                try:os.killpg(self.pid,signal.SIGKILL)
                except ProcessLookupError:pass
    kill = terminate

    def wait(self, timeout=None):
        deadline=None if timeout is None else time.monotonic()+timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic()>=deadline:raise subprocess.TimeoutExpired('owned process',timeout)
            time.sleep(.02)
        return self.returncode if os.name=='nt' else self.proc.returncode

    def close(self):
        with self.lock:
            if os.name!='nt':
                if hasattr(self,'proc'):
                    self.terminate()
                    try:self.proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:pass
                return
            if self.job:
                self.k.TerminateJobObject(self.job,1);self.k.CloseHandle(self.job);self.job=None
            if self.handle:
                self.k.WaitForSingleObject(self.handle,2000)
                self.k.CloseHandle(self.handle);self.handle=None
            if self.returncode is None:self.returncode=1


def watch_parent(parent_pid, on_death):
    """Bind to a process handle, not a PID which could later be reused."""
    if not parent_pid:return
    if os.name=='nt':
        from ctypes import wintypes as w
        k=ctypes.WinDLL('kernel32',use_last_error=True)
        k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.WaitForSingleObject.restype=w.DWORD
        k.CloseHandle.argtypes=[w.HANDLE]
        handle=k.OpenProcess(0x100000,False,int(parent_pid))
        if not handle:on_death();return
        def wait():
            try:k.WaitForSingleObject(handle,0xFFFFFFFF);on_death()
            finally:k.CloseHandle(handle)
    else:
        def wait():
            while os.getppid()==parent_pid:time.sleep(.1)
            on_death()
    threading.Thread(target=wait,daemon=True,name='UDB parent lifetime').start()
