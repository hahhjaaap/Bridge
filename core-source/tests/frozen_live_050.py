"""Actual frozen GUI + frozen stdio + bundled Python + Explorer acceptance."""
import asyncio,base64,json,subprocess,sys,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
from udb.core import Agent

def parse(r):
    if r.isError:raise RuntimeError(str([getattr(c,'text','') for c in r.content])[:1000])
    if isinstance(r.structuredContent,dict):return r.structuredContent
    for c in r.content:
        if c.type=='text':
            try:return json.loads(c.text)
            except ValueError:pass
    raise RuntimeError('No JSON response')

async def main():
    sys.stdout.reconfigure(encoding='utf8',errors='replace')
    installed=json.loads((ROOT/'docs'/'installer-acceptance.json').read_text('utf8'))
    exe=Path(installed['installed_exe'])
    home=ROOT.parent/'release-050-dev2-r2'/'frozen-live-home-3'
    fixture=home.parent/'frozen-source-fixture-3'
    if home.exists() or fixture.exists():raise RuntimeError('Use a fresh frozen QA home')
    fixture.mkdir();(fixture/'hello.py').write_text('value = 7\n',encoding='utf8')
    a=Agent(home);a.register_local('qa',str(fixture),True)
    a.set_sandbox_autonomy_local('qa',True) # Isolated synthetic fixture only, not a production grant.
    gui=subprocess.Popen([str(exe),'--home',str(home)],creationflags=subprocess.CREATE_NO_WINDOW)
    report={'version':'0.5.0-dev.2','exe':str(exe),'scope':'isolated fixture; real frozen GUI/MCP/guest runtime, not ChatGPT registry'}
    sid=None
    try:
        async with stdio_client(StdioServerParameters(command=str(exe),args=['--stdio','--home',str(home)])) as (r,w):
            async with ClientSession(r,w) as s:
                await s.initialize()
                catalog=await s.list_tools();report['tool_count']=len(catalog.tools);assert report['tool_count']==24
                async def call(tool_name,**kw):
                    t=time.perf_counter();value=parse(await s.call_tool(tool_name,kw))
                    print(tool_name,round((time.perf_counter()-t)*1000,2),'ms',flush=True);return value
                many=await call('read_files',project='qa',paths=['hello.py']);assert len(many['files'])==1
                found=await call('search_files',project='qa',pattern='*.py',text='value')
                assert found['items'][0]['path']=='hello.py'
                row=await call('read_file',project='qa',path='hello.py')
                p=await call('propose_text_edit',project='qa',path='hello.py',old_text='7',new_text='9',expected_sha256=row['sha256'])
                assert p['status']=='pending_local_approval'
                assert (fixture/'hello.py').read_text()=='value = 7\n'
                report['source_tools_and_pending_write']=True
                result=await call('propose_desktop_session',project='qa',
                    task='Isolated frozen EXE and portable runtime acceptance with real File Explorer Unicode and batch',duration_seconds=260)
                assert result['status']=='queued_sandbox';sid=result['request_id']
                for _ in range(95):
                    req=await call('get_request_status',request_id=sid)
                    if req['status']=='started':
                        st=await call('desktop_status',session_id=sid)
                        if st['ready']:break
                    elif req['status'] in ('failed','rejected','expired'):raise RuntimeError(str(req))
                    await asyncio.sleep(1)
                else:raise RuntimeError('Frozen guest not ready')
                report['portable_runtime_guest_started']=True
                await call('desktop_app',session_id=sid,operation='open',app='explorer')
                window_id=None
                for _ in range(40):
                    windows=(await call('desktop_app',session_id=sid,operation='list'))['state']['windows']
                    matches=[r for r in windows if r['class_name']=='CabinetWClass']
                    if matches:window_id=matches[0]['window_id'];break
                    await asyncio.sleep(.2)
                if not window_id:raise RuntimeError('Explorer missing')
                focused=await call('desktop_app',session_id=sid,operation='focus',window_id=window_id)
                assert focused['state']['verified'];report['explorer_focus']=True
                snap=await call('desktop_snapshot',session_id=sid,window_id=window_id)
                targets=[r for r in snap['state']['elements'] if r['control_type']=='Edit' and 'search' in (r['name']+r['automation_id']).casefold()]
                assert len(targets)==1
                edited=await call('desktop_element_action',session_id=sid,operation='set_text',window_id=window_id,
                    automation_id=targets[0]['automation_id'],name=targets[0]['name'],control_type=targets[0]['control_type'],value='Kiểm thử EXE tiếng Việt')
                assert edited['state']['verified'];report['explorer_unicode_verified']=True
                t=time.perf_counter()
                batch=await call('desktop_batch',session_id=sid,steps=[{'action':'snapshot','window_id':window_id}]*12)
                assert batch['state']['ok'] and batch['state']['completed']==12
                report['twelve_snapshot_batch']={'completed':12,'ms':round((time.perf_counter()-t)*1000,3),
                    'json_bytes':len(json.dumps(batch).encode())}
                shot=await s.call_tool('desktop_screenshot',{'session_id':sid,'x':200,'y':79,'width':1000,'height':600,'max_width':1000})
                assert not shot.isError
                for block in shot.content:
                    if block.type=='image':(ROOT/'docs'/'frozen-explorer.png').write_bytes(base64.b64decode(block.data))
                report['guest_screenshot']=True
                await call('desktop_stop',session_id=sid)
                for _ in range(30):
                    st=await call('desktop_status',session_id=sid)
                    if st['status'] not in ('running','reviewing','stopping'):break
                    await asyncio.sleep(.5)
                else:raise RuntimeError('Guest did not stop')
                report['stopped']=True
    except Exception as exc:
        report['error']=str(exc);print(traceback.format_exc(),flush=True)
    finally:
        if sid:
            try:
                from udb.desktop_vm import stop
                stop(a,sid)
                for _ in range(30):
                    with a.connect() as c:row=c.execute('SELECT status FROM jobs WHERE id=?',(sid,)).fetchone()
                    if not row or row[0] not in ('running','reviewing','stopping'):break
                    await asyncio.sleep(.5)
            except Exception:pass
        if gui.poll() is None:subprocess.run(['taskkill','/PID',str(gui.pid),'/T','/F'],capture_output=True)
        (ROOT/'docs'/'frozen-live-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print('FROZEN_REPORT',json.dumps(report,ensure_ascii=True),flush=True)
    if report.get('error'):raise SystemExit(1)
if __name__=='__main__':asyncio.run(main())
