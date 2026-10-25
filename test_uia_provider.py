"""Windows-only regression through the real MCP desktop tool; own form only."""
import itertools
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

ROOT=Path(__file__).resolve().parent


def receive(stream,rows):
    for line in iter(stream.readline,b''):
        try:rows.put(json.loads(line.decode('utf-8')))
        except (ValueError,UnicodeError):pass


class Client:
    def __init__(self):
        from mcp_test_config import configuration
        config=configuration()
        expand=lambda s:s.replace('${PLUGIN_ROOT}',str(ROOT))
        self.p=subprocess.Popen([expand(config['command']),*[expand(a) for a in config['args']]],cwd=ROOT,
                                stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        self.rows=queue.Queue();self.ids=itertools.count(1)
        self.reader=threading.Thread(target=receive,args=(self.p.stdout,self.rows),daemon=True);self.reader.start()
        self.rpc('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo=dict(name='UDB UIA regression',version='1')))
        self.p.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n');self.p.stdin.flush()
    def rpc(self,method,params,allow_error=False):
        ident=next(self.ids)
        self.p.stdin.write((json.dumps(dict(jsonrpc='2.0',id=ident,method=method,params=params),ensure_ascii=False)+'\n').encode());self.p.stdin.flush()
        deadline=time.monotonic()+30
        while True:
            row=self.rows.get(timeout=max(.01,deadline-time.monotonic()))
            if row.get('id')==ident:
                if allow_error and 'error' in row:return row
                assert 'error' not in row,row
                return row['result']
    def tool(self,**args):return self.rpc('tools/call',dict(name='computer_desktop',arguments=args))
    def close(self):
        self.p.stdin.close()
        try:self.p.wait(timeout=15)
        except subprocess.TimeoutExpired:self.p.terminate();self.p.wait(timeout=5)
        self.reader.join(timeout=2);self.p.stdout.close()


def object_data(result):
    assert not result.get('isError'),result
    if isinstance(result.get('structuredContent'),dict):return result['structuredContent']
    for row in result.get('content',[]):
        try:return json.loads(row.get('text',''))
        except ValueError:pass
    raise AssertionError('No object result')


def main():
    if os.name!='nt':raise RuntimeError('Native Windows required')
    powershell=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
    # PowerShell 5 reads BOM-less .ps1 as ANSI; send decoded Unicode explicitly.
    script=(ROOT/'uia_provider_fixture.ps1').read_text(encoding='utf-8-sig')
    form=subprocess.Popen([powershell,'-NoProfile','-NonInteractive','-Sta','-Command',script],
                          stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    rows=queue.Queue();reader=threading.Thread(target=receive,args=(form.stdout,rows),daemon=True);reader.start()
    client=None
    try:
        ready=rows.get(timeout=15)
        assert ready['event']=='ready'
        client=Client()
        windows=object_data(client.tool(action='list'))['windows']
        owned=[w for w in windows if w['pid']==form.pid and w['title']==ready['title']]
        assert len(owned)==1,'Cannot identify the test-owned form'
        wid=owned[0]['window_id']
        snapshot=object_data(client.tool(action='snapshot',window_id=wid))
        controls={e['automation_id']:e['control_type'] for e in snapshot['elements']}
        assert controls['OrderName']=='ControlType.Edit' and controls['Calculate']=='ControlType.Button',controls
        for control,text in [('OrderName','Nghiệm thu provider 🚀'),('Quantity','4'),('UnitPrice','125000')]:
            row=object_data(client.tool(action='set_value',window_id=wid,automation_id=control,control_type='Edit',text=text))
            assert row['verified_control_value'] and row['method']=='ValuePattern',row
        assert object_data(client.tool(action='invoke',window_id=wid,automation_id='Calculate',control_type='Button'))['invoked']
        result=rows.get(timeout=5)
        assert result==dict(event='calculate',output='Nghiệm thu provider 🚀 | Tổng tiền: 500000 VND'),result
        assert client.tool(action='invoke',window_id=wid,automation_id='does-not-exist',control_type='Button').get('isError')
        assert object_data(client.tool(action='invoke',window_id=wid,automation_id='Reset',control_type='Button'))['invoked']
        assert rows.get(timeout=5)['event']=='reset'
        assert object_data(client.tool(action='invoke',window_id=wid,automation_id='CloseTest',control_type='Button'))['invoked']
        assert rows.get(timeout=5)['event']=='closed'
        assert form.wait(timeout=5)==0
        print('PASS | Standard providers recognize Edit/Button; ValuePattern Unicode readback; real InvokePattern changes app state; missing selector refused; own form closed')
    finally:
        if form.poll() is None:form.terminate();form.wait(timeout=5)
        reader.join(timeout=2);form.stdout.close()
        if client:client.close()


if __name__=='__main__':main()
