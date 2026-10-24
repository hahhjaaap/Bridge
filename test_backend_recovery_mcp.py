"""Crash only children of an owned real proxy; verify subsequent MCP calls."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from test_uia_provider import Client,object_data


def terminate_backend(proxy_pid,kind):
    powershell=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
    script="$rows=@(Get-CimInstance Win32_Process -Filter 'ParentProcessId="+str(proxy_pid)+"' -Property ProcessId,ExecutablePath,CreationDate,CommandLine);$rows|ForEach-Object {@{pid=$_.ProcessId;image=$_.ExecutablePath;creation=$_.CreationDate.ToFileTimeUtc();role=$(if($_.CommandLine -like '*--stdio*'){'original'}elseif($_.CommandLine -like '*udb_companion.py*'){'companion'}else{'unknown'})}}|ConvertTo-Json -Compress"
    rows=json.loads(subprocess.run([powershell,'-NoProfile','-NonInteractive','-Command',script],capture_output=True,text=True,check=True,timeout=15).stdout)
    if isinstance(rows,dict):rows=[rows]
    selected=[r for r in rows if r['role']==kind]
    assert len(selected)==1,'Not one test-owned backend'
    row=selected[0];k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
    k.QueryFullProcessImageNameW.argtypes=[w.HANDLE,w.DWORD,w.LPWSTR,ctypes.POINTER(w.DWORD)]
    k.GetProcessTimes.argtypes=[w.HANDLE,*([ctypes.c_void_p]*4)]
    k.TerminateProcess.argtypes=[w.HANDLE,w.UINT];k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.CloseHandle.argtypes=[w.HANDLE]
    handle=k.OpenProcess(0x100000|0x1000|1,False,row['pid']);assert handle
    try:
        times=(ctypes.c_uint64*4)();assert k.GetProcessTimes(handle,*[ctypes.byref(times,i*8) for i in range(4)])
        assert abs(int(times[0])-row['creation'])<=10000,'PID identity changed'
        image=ctypes.create_unicode_buffer(32768);length=w.DWORD(len(image))
        assert k.QueryFullProcessImageNameW(handle,0,image,ctypes.byref(length))
        assert Path(image.value)==Path(row['image'])
        assert k.TerminateProcess(handle,12);assert k.WaitForSingleObject(handle,5000)==0
    finally:k.CloseHandle(handle)


def main():
    if os.name!='nt':raise RuntimeError('Windows required')
    client=Client()
    def tool(name,**args):return client.rpc('tools/call',dict(name=name,arguments=args))
    try:
        tools=client.rpc('tools/list',{})['tools'];assert len(tools)==58
        assert not tool('bridge_status').get('isError')
        terminate_backend(client.p.pid,'original')
        assert not tool('bridge_status').get('isError'),'Real original backend did not recover'
        start=tool('terminal_start',program=sys.executable,arguments=['-u','-c',"import os,time;print('TEST_PID',os.getpid(),flush=True);time.sleep(90)"],wait_ms=200)
        raw='\n'.join(b.get('text','') for b in start.get('content',[]));sid=re.search(r'Phiên: ([a-f0-9]{12})',raw).group(1)
        for _ in range(50):
            match=re.search(r'TEST_PID (\d+)',raw)
            if match:break
            time.sleep(.1)
            raw+='\n'+'\n'.join(b.get('text','') for b in tool('terminal_read',session_id=sid,offset=0,wait_ms=200).get('content',[]))
        assert match,'No test terminal PID: '+raw
        k=ctypes.WinDLL('kernel32');k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
        k.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];k.CloseHandle.argtypes=[w.HANDLE]
        process=k.OpenProcess(0x100000,False,int(match.group(1)));assert process
        try:
            terminate_backend(client.p.pid,'companion')
            assert k.WaitForSingleObject(process,5000)==0,'Kernel did not stop fixture terminal tree'
        finally:k.CloseHandle(process)
        sessions=tool('terminal_list');assert not sessions.get('isError')
        assert 'No companion-owned terminal sessions' in json.dumps(sessions)
        assert tool('terminal_read',session_id=sid).get('isError'),'Lost session must not be replayed/reattached'
        status=tool('bridge_status');health=None
        for b in status['content']:
            try:
                value=json.loads(b.get('text',''))
                if 'backend_health' in value:health=value['backend_health']
            except ValueError:pass
        assert health and health['original']['generation']>=2 and health['companion']['generation']>=2,health
        assert not health['original']['replay_enabled'] and not health['companion']['replay_enabled']
        print('PASS | Real proxy recovers original and Companion backends; kernel stops lost terminal, old ID refused, no session replay')
    finally:client.close()


if __name__=='__main__':main()
