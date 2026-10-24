"""Windows acceptance: only fixture-owned processes/handles, never user tasks."""
import ctypes
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT=Path(__file__).resolve().parent


class Client:
    def __init__(self,root,parent_pid=0):
        self.p=subprocess.Popen([sys.executable,'-u',str(ROOT/'udb_companion.py'),'--root',str(root),'--enable-terminal','--parent-pid',str(parent_pid)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)
        self.q=queue.Queue();self.seq=0
        def receive():
            try:
                for line in iter(self.p.stdout.readline,b''):
                    try:self.q.put(json.loads(line))
                    except ValueError:self.q.put({'bad':True})
            except (OSError,ValueError):pass
        self.receiver=threading.Thread(target=receive,daemon=True);self.receiver.start()
        self.rpc('initialize',{})
    def rpc(self,method,params):
        self.seq+=1;self.p.stdin.write((json.dumps(dict(jsonrpc='2.0',id=self.seq,method=method,params=params))+'\n').encode());self.p.stdin.flush()
        while True:
            row=self.q.get(timeout=10)
            if row.get('id')==self.seq:
                if 'error' in row:raise AssertionError(row)
                return row['result']
    def tool(self,name,args):
        row=self.rpc('tools/call',dict(name=name,arguments=args))
        if row.get('isError'):raise AssertionError(row)
        return row
    def tree(self,timeout_seconds=3600):
        code="import subprocess,sys,time,os,json;p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']);print(json.dumps({'root':os.getpid(),'child':p.pid}),flush=True);time.sleep(120)"
        row=self.tool('terminal_start',dict(program=sys.executable,arguments=['-u','-c',code],wait_ms=200,timeout_seconds=timeout_seconds))
        text=row['content'][0]['text'];sid=re.search(r'Phiên: ([a-f0-9]{12})',text)
        assert sid,text
        for _ in range(25):
            match=re.search(r'\{"root": \d+, "child": \d+\}',text)
            if match:return json.loads(match.group()),sid.group(1)
            text=self.tool('terminal_read',dict(session_id=sid.group(1),wait_ms=100))['content'][0]['text']
        raise AssertionError('Owned tree did not report its PIDs')
    def close(self):
        if self.p.poll() is None:
            self.p.stdin.close()
            try:self.p.wait(timeout=10)
            except subprocess.TimeoutExpired:self.p.terminate();self.p.wait(timeout=5)
        self.receiver.join(timeout=1)
        self.p.stdin.close();self.p.stdout.close()


def kernel():
    from ctypes import wintypes as w
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    for n,ret,args in [('OpenProcess',w.HANDLE,[w.DWORD,w.BOOL,w.DWORD]),('WaitForSingleObject',w.DWORD,[w.HANDLE,w.DWORD]),('CloseHandle',w.BOOL,[w.HANDLE]),('TerminateProcess',w.BOOL,[w.HANDLE,w.UINT])]:
        f=getattr(k,n);f.restype=ret;f.argtypes=args
    return k


@unittest.skipUnless(os.name=='nt','Windows kernel Job Object acceptance')
class WindowsDurability(unittest.TestCase):
    def setUp(self):self.temp=tempfile.TemporaryDirectory(prefix='udb-job-qa-');self.root=Path(self.temp.name);self.k=kernel();self.handles=[]
    def observe(self,pid):
        handle=self.k.OpenProcess(0x100000|0x1000|1,False,pid)
        self.assertTrue(handle,'Could not observe owned fixture');self.handles.append(handle)
        self.assertEqual(self.k.WaitForSingleObject(handle,0),258)
        return handle
    def stopped(self,handles):
        for handle in handles:self.assertEqual(self.k.WaitForSingleObject(handle,4000),0,'Owned descendant survived')
    def tearDown(self):
        for h in self.handles:
            if self.k.WaitForSingleObject(h,0)==258:self.k.TerminateProcess(h,1)
            self.k.CloseHandle(h)
        self.temp.cleanup()

    def test_normal_stop_and_eof_clean_entire_tree(self):
        client=Client(self.root)
        try:
            ids,sid=client.tree();handles=[self.observe(ids[x]) for x in ('root','child')]
            client.tool('terminal_stop',dict(session_id=sid));self.stopped(handles)
            ids,sid=client.tree();handles=[self.observe(ids[x]) for x in ('root','child')]
            client.close();self.stopped(handles)
        finally:client.close()

    def test_terminal_timeout_cleans_entire_tree(self):
        client=Client(self.root)
        try:
            ids,sid=client.tree(timeout_seconds=1);handles=[self.observe(ids[x]) for x in ('root','child')]
            self.stopped(handles)
        finally:client.close()

    def test_abrupt_companion_kill_closes_kernel_job(self):
        client=Client(self.root)
        try:
            ids,sid=client.tree();handles=[self.observe(ids[x]) for x in ('root','child')]
            client.p.terminate();client.p.wait(timeout=5);self.stopped(handles)
        finally:client.close()

    def test_abrupt_proxy_parent_death_stops_companion_and_tree(self):
        harness=subprocess.Popen([sys.executable,'-u',str(Path(__file__).resolve()),'--harness',str(self.root)],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        try:
            q=queue.Queue();threading.Thread(target=lambda:q.put(harness.stdout.readline()),daemon=True).start()
            ids=json.loads(q.get(timeout=15));handles=[self.observe(ids[x]) for x in ('companion','root','child')]
            harness.terminate();harness.wait(timeout=5);self.stopped(handles)
        finally:
            if harness.poll() is None:harness.terminate();harness.wait(timeout=5)
            harness.stdout.close()

    def test_job_limits_and_auth_environment(self):
        from udb_managed_process import ManagedProcess
        proc=ManagedProcess([sys.executable,'-u','-c',"import os,time;print(int('CONTROL_PLANE_API_KEY' in os.environ or 'TUNNEL_CLIENT_PROFILE_DIR' in os.environ),flush=True);time.sleep(20)"],str(self.root),256)
        try:
            self.assertEqual(proc.stdout.readline().strip(),b'0')
            self.k.QueryInformationJobObject.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,ctypes.c_uint32,ctypes.c_void_p]
            self.k.QueryInformationJobObject.restype=ctypes.c_int
            buf=ctypes.create_string_buffer(144)
            self.assertTrue(self.k.QueryInformationJobObject(proc.job,9,buf,144,None))
            flags=int.from_bytes(buf.raw[16:20],'little')
            self.assertEqual(flags & (0x2000|0x8|0x200),0x2000|0x8|0x200)
            self.assertEqual(int.from_bytes(buf.raw[120:128],'little'),256*1048576)
        finally:
            proc.close();proc.stdin.close();proc.stdout.close()


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--harness':
        client=Client(sys.argv[2],os.getpid());ids,sid=client.tree();ids['companion']=client.p.pid
        print(json.dumps(ids),flush=True);time.sleep(120)
    else:unittest.main(verbosity=2)
