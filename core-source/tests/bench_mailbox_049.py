"""Real guest fixed-mailbox latency benchmark, isolated demo project."""
import asyncio,json,time,sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
ROOT=Path(__file__).resolve().parents[1]
def parse(result):
    if result.isError:raise RuntimeError(str([getattr(c,'text','') for c in result.content])[:700])
    if isinstance(result.structuredContent,dict):return result.structuredContent
    for c in result.content:
        if c.type=='text':
            try:return json.loads(c.text)
            except (ValueError,TypeError):pass
    raise RuntimeError('No JSON tool response')
async def main():
    sys.stdout.reconfigure(encoding='utf8',errors='replace')
    params=StdioServerParameters(command=str(ROOT/'UniversalDesktopBridge.exe'),
        args=['--stdio','--home',str(ROOT)])
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            async def call(tool_name,**kwargs):
                t=time.monotonic()
                data=parse(await s.call_tool(tool_name,kwargs))
                print('CALL',tool_name,'SECONDS',round(time.monotonic()-t,3),flush=True)
                return data
            p=await call('propose_desktop_session',project='demo',
                task='Benchmark mailbox latency and verify UDB Sandbox Pad controls in isolated Windows Sandbox only',
                duration_seconds=260)
            sid=p['request_id']
            print('SESSION',sid,p['status'],flush=True)
            try:
                if p['status']!='queued_sandbox':
                    print('NOT_AUTO_GRANTED_ABORT',flush=True);return
                for i in range(85):
                    req=await call('get_request_status',request_id=sid) if i%3==0 else req
                    if req['status'] in ('failed','rejected','expired'):
                        print('REQUEST_STOP',req['status'],flush=True);return
                    if req['status']!='started':
                        await asyncio.sleep(1)
                        continue
                    st=await call('desktop_status',session_id=sid)
                    if st.get('ready'):
                        print('READY MAILBOX_PROTOCOL',st.get('mailbox_protocol',0),flush=True)
                        break
                    await asyncio.sleep(1)
                else:print('READY_TIMEOUT',flush=True);return
                for i in range(3):
                    state=await call('desktop_action',session_id=sid,action='state')
                    print('STATE_OK',i,state.get('ok'),flush=True)
                await call('desktop_action',session_id=sid,action='open_demo')
                snap=await call('desktop_snapshot',session_id=sid)
                elements=snap.get('state',{}).get('elements',[])
                edits=[x for x in elements if x.get('automation_id') and 'EDIT' in x.get('native_class','').upper()]
                buttons=[x for x in elements if x.get('automation_id') and x.get('name')=='Confirm']
                print('UIA_CONTROLS',len(elements),len(edits),len(buttons),flush=True)
                if len(edits)!=1 or len(buttons)!=1:raise RuntimeError('ambiguous UIA controls')
                await call('desktop_element_action',session_id=sid,operation='set_text',
                           automation_id=edits[0]['automation_id'],value='UDB_MAILBOX_049_OK')
                await call('desktop_wait_for',session_id=sid,automation_id=buttons[0]['automation_id'],name='Confirm',timeout_seconds=6)
                await call('desktop_element_action',session_id=sid,operation='invoke',
                           automation_id=buttons[0]['automation_id'],name='Confirm')
                snap=await call('desktop_snapshot',session_id=sid)
                names=[x.get('name','') for x in snap.get('state',{}).get('elements',[])]
                passed=any('CONFIRMED: UDB_MAILBOX_049_OK' in x for x in names)
                print('GUEST_VERIFIED',passed,flush=True)
                if not passed:raise RuntimeError('Missing guest confirmation')
            finally:
                try:
                    print('STOP',await call('desktop_stop',session_id=sid),flush=True)
                except Exception as exc:
                    print('STOP_FAILED',str(exc)[:400],flush=True)
if __name__=='__main__':asyncio.run(main())
