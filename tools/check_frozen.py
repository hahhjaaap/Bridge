"""Exercise the standalone CLI and real MCP worker tree without owner grants."""
import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
import time


def main():
    parser=argparse.ArgumentParser();parser.add_argument('executable',type=Path);args=parser.parse_args()
    executable=args.executable.resolve()
    env={k:v for k,v in os.environ.items() if not k.startswith('_PYI_') and k not in {'_MEIPASS2','CONTROL_PLANE_API_KEY','TUNNEL_CLIENT_PROFILE_DIR','PYTHONPATH','UDB_HOME'}}
    env['PYINSTALLER_RESET_ENVIRONMENT']='1';env['PYTHONIOENCODING']='utf-8'
    with tempfile.TemporaryDirectory(prefix='udb-frozen-') as temporary:
        root=Path(temporary);home=root/'home';project=root/'project';home.mkdir();project.mkdir()
        def run(*arguments):
            result=subprocess.run([str(executable),*arguments],cwd=root,env=env,capture_output=True,text=True,timeout=90)
            assert result.returncode==0,result.stderr
            return result.stdout
        version=run('--version').strip()
        catalog=json.loads(run('tools'));manifest=json.loads(run('manifest'));info=json.loads(run('build-info'))
        assert catalog['version']==manifest['version']==info['version']==version
        assert catalog['tool_count']==60 and catalog['backend_count']==66
        if os.name=='nt':assert info['kind']=='frozen' and info['release_provenance']
        process=subprocess.Popen([str(executable),'serve','--home',str(home),'--root',str(project),'--enable-terminal'],cwd=root,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
        rows=queue.Queue();sequence=0
        def receive():
            for line in iter(process.stdout.readline,b''):
                try:rows.put(json.loads(line))
                except ValueError:rows.put({'invalid_frame':True})
        thread=threading.Thread(target=receive,daemon=True);thread.start()
        def rpc(method,params):
            nonlocal sequence
            sequence+=1
            process.stdin.write((json.dumps(dict(jsonrpc='2.0',id=sequence,method=method,params=params))+'\n').encode());process.stdin.flush()
            deadline=time.monotonic()+90
            while True:
                row=rows.get(timeout=max(.01,deadline-time.monotonic()))
                assert not row.get('invalid_frame'),row
                if row.get('id')==sequence:return row
        try:
            initial=rpc('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo=dict(name='standalone-acceptance',version='1')))
            assert initial['result']['serverInfo']['version']==version
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n');process.stdin.flush()
            assert len(rpc('tools/list',{})['result']['tools'])==60
            status=rpc('tools/call',dict(name='bridge_status',arguments={}))['result']
            assert not status.get('isError') and status['_meta']['udb']['version']==version
            denied=rpc('tools/call',dict(name='terminal_start',arguments=dict(program=str(executable),arguments=['--version'])))
            assert 'error' in denied or denied.get('result',{}).get('isError'),denied
            assert not (project/'.udb-work-state').exists(),'Denied request created work records'
            assert not (home/'UniversalDesktopBridge.exe').exists()
            print(json.dumps(dict(result='PASS',version=version,tools=60,manifest=True,real_mcp_workers=True,owner_access_denied=True,external_core_exe_required=False)))
        finally:
            process.stdin.close()
            try:process.wait(timeout=30)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            thread.join(timeout=2);process.stdout.close()


if __name__=='__main__':main()
