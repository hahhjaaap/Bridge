"""Installed-protocol acceptance: real ConPTY and explicit recovery after crash."""
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from test_uia_provider import Client,ROOT,object_data
from udb_background import version
from udb_work_journal import Journal

def main():
    args=json.loads((ROOT/'mcp.json').read_text())['mcpServers']['universal-desktop-bridge']['args'];project=Path(args[args.index('--root')+1])
    records=[];client=None
    with tempfile.TemporaryDirectory(prefix='udb-next-mcp-',dir=project) as folder:
        prefix=Path(folder).relative_to(project).as_posix();source=Path(folder)/'source.txt';source.write_text('Recovery qua MCP',encoding='utf-8')
        recovery=dict(tool='file_operation',arguments=dict(action='copy',source=prefix+'/source.txt',destination=prefix+'/copy.txt'),source_version=version(source))
        script="import sys,json,time,os;sys.path.insert(0,sys.argv[1]);from udb_companion import Runtime;rt=Runtime(sys.argv[2]);r=rt.features.background.new('file_copy',lambda t:time.sleep(90),recovery=json.loads(sys.argv[3]));print(r['job_id'],flush=True);os._exit(12)"
        crash=subprocess.run([sys.executable,'-u','-c',script,str(ROOT),str(project),json.dumps(recovery)],capture_output=True,text=True,timeout=15)
        assert crash.returncode==12,crash.stderr;jid=crash.stdout.strip();records.append('job-'+jid)
        try:
            client=Client()
            def call(name,**a):return client.rpc('tools/call',dict(name=name,arguments=a))
            tools=client.rpc('tools/list',{})['tools'];assert len(tools)==60
            native=call('terminal_start',program=sys.executable,arguments=['-u','-c',"import time;print('CONPTY_MCP_READY',flush=True);input();print('CONPTY_MCP_DONE',flush=True)"],cwd=prefix,mode='conpty',columns=95,rows=28,wait_ms=0)
            assert not native.get('isError'),native
            text='\n'.join(b.get('text','') for b in native['content']);sid=re.search('Phiên: ([a-f0-9]{12})',text).group(1);records.append('terminal-'+sid)
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                text='\n'.join(b.get('text','') for b in call('terminal_read',session_id=sid,offset=0,wait_ms=100)['content'])
                if 'CONPTY_MCP_READY' in text:break
                time.sleep(.05)
            assert 'CONPTY_MCP_READY' in text
            data=object_data(call('terminal_interact',session_id=sid,text='ok\r',columns=120,rows=35,wait_ms=200))
            assert data['terminal_mode']=='conpty' and data['output_format']=='vt_utf8'
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                data=object_data(call('terminal_interact',session_id=sid,offset=0,wait_ms=100))
                if data['state']=='PASS':break
            assert data['state']=='PASS' and 'CONPTY_MCP_DONE' in data['output'],data
            old=object_data(call('work_recovery',action='inspect',kind='job',work_id=jid));assert old['state']=='interrupted'
            new=object_data(call('work_recovery',action='resume',kind='job',work_id=jid));records.append('job-'+new['job_id'])
            deadline=time.monotonic()+10;row=new
            while row.get('state')=='running' and time.monotonic()<deadline:
                time.sleep(.05);row=object_data(call('file_operation',action='read',job_id=new['job_id']))
            assert row['state']=='completed',row
            assert (Path(folder)/'copy.txt').read_text(encoding='utf-8')=='Recovery qua MCP'
            assert object_data(call('work_recovery',action='resume',kind='job',work_id=jid))['already_resumed']
            client.close();client=None
            client=Client();past=object_data(call('work_recovery',action='inspect',kind='terminal',work_id=sid));assert past['process_restorable'] is False
            assert call('work_recovery',action='resume',kind='terminal',work_id=sid).get('isError')
            print('PASS | Real MCP ConPTY start/VT stdin/resize/exit; recover copy after fixture crash, duplicate resume same ID, saved terminal after new backend; no terminal replay')
        finally:
            if client:client.close()
            journal=Journal(project)
            for key in records:
                lease=journal.lease(key)
                try:journal.file(key).unlink(missing_ok=True)
                finally:lease.close()
if __name__=='__main__':main()
