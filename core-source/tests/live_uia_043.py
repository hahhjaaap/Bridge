"""Bounded real-guest UI Automation smoke, demo project only. Never approves a request."""
import asyncio, json, sys, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HOME = Path(__file__).resolve().parents[1]
EXE = HOME/'UniversalDesktopBridge.exe'

def data(result):
    if result.isError:
        raise RuntimeError('MCP: '+repr([getattr(p,'text','') for p in result.content])[:600])
    if isinstance(result.structuredContent,dict):
        return result.structuredContent
    for part in result.content:
        if getattr(part,'type',None)=='text':
            try: return json.loads(part.text)
            except ValueError: continue
    raise RuntimeError('No JSON in tool result')

async def main():
    sys.stdout.reconfigure(encoding='utf8',errors='replace')
    args=['--stdio','--home',str(HOME)]
    async with stdio_client(StdioServerParameters(command=str(EXE),args=args)) as (reader,writer):
        async with ClientSession(reader,writer) as client:
            await client.initialize()
            async def call(tool,arguments):
                started=time.monotonic()
                try:
                    return data(await client.call_tool(tool,arguments))
                finally:
                    print('TOOL_SECONDS',tool,round(time.monotonic()-started,3),flush=True)
            proposal=await call('propose_desktop_session',{'project':'demo',
                'task':'Open UDB Sandbox Pad, inspect accessible controls, invoke Confirm, then stop guest',
                'duration_seconds':180})
            rid=proposal['request_id']
            print('PROPOSE',json.dumps({'request_id':rid,'status':proposal['status']}),flush=True)
            try:
                if proposal['status']!='queued_sandbox':
                    print('NO_AUTONOMY_GRANT_ABORT',flush=True)
                    return
                until=time.monotonic()+85
                while time.monotonic()<until:
                    req=await call('get_request_status',{'request_id':rid})
                    if req['status'] in ('started','failed','rejected','expired'):
                        print('REQUEST',req['status'],str(req.get('result',{}))[:250],flush=True)
                        break
                    await asyncio.sleep(1)
                else:
                    print('REQUEST_TIMEOUT',flush=True)
                    return
                if req['status']!='started':return
                until=time.monotonic()+85
                while time.monotonic()<until:
                    s=await call('desktop_status',{'session_id':rid})
                    if s.get('ready'):break
                    if s.get('status') in ('failed','stopped','timeout','completed'):
                        print('SESSION_FAILED',str(s)[:500],flush=True)
                        return
                    await asyncio.sleep(1)
                else:
                    print('READY_TIMEOUT',flush=True)
                    return
                print('GUEST_READY',flush=True)
                try:
                    a=await call('desktop_action',{'session_id':rid,'action':'open_demo'})
                    print('OPEN_DEMO',str(a.get('ok')),flush=True)
                    snap=await call('desktop_snapshot',{'session_id':rid})
                    elements=snap.get('state',{}).get('elements',[])
                    print('SNAPSHOT',json.dumps({'count':len(elements),'elements':elements[:15]},ensure_ascii=False),flush=True)
                    textboxes=[e for e in elements if not e.get('name') and e.get('automation_id') and e.get('bounds',[0,0,0,0])[3] >= 80]
                    buttons=[e for e in elements if e.get('name') == 'Confirm' and e.get('automation_id')]
                    if len(textboxes)!=1 or len(buttons)!=1:
                        raise RuntimeError('Demo controls not uniquely identifiable by snapshot')
                    text_id=textboxes[0]['automation_id']
                    button_id=buttons[0]['automation_id']
                    typed=await call('desktop_element_action',{'session_id':rid,'operation':'set_text','automation_id':text_id,'value':'UDB_UIA_046_OK'})
                    print('SET_TEXT',str(typed.get('ok')),flush=True)
                    found=await call('desktop_wait_for',{'session_id':rid,'name':'Confirm','automation_id':button_id,'timeout_seconds':7})
                    print('WAIT_FOR',str(found.get('ok')),flush=True)
                    invoked=await call('desktop_element_action',{'session_id':rid,'operation':'invoke','name':'Confirm','automation_id':button_id})
                    print('INVOKE',str(invoked.get('ok')),flush=True)
                    final=await call('desktop_snapshot',{'session_id':rid})
                    labels=[e.get('name','') for e in final.get('state',{}).get('elements',[])]
                    success=any('CONFIRMED: UDB_UIA_046_OK' in x for x in labels)
                    print('UIA_VERIFIED',success,flush=True)
                    if not success: raise RuntimeError('No confirmation marker in guest UIA snapshot')
                except Exception as exc:
                    print('UIA_TEST_ERROR',str(exc)[:650],flush=True)
            finally:
                try:
                    stop=await call('desktop_stop',{'session_id':rid})
                    print('STOP',str(stop.get('status')),flush=True)
                except Exception as exc:
                    print('STOP_ERROR',str(exc)[:500],flush=True)
if __name__=='__main__':
    asyncio.run(main())
