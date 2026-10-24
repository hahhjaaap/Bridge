"""Read-only diagnostics of one owner-authorized Windows Sandbox guest session."""
import asyncio,json,sys,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT=Path(__file__).resolve().parents[1]
async def call(s,tool,arguments):
    r=await s.call_tool(tool,arguments)
    if r.isError:
        raise RuntimeError(str([x.text for x in r.content if x.type=='text'])[:500])
    if r.structuredContent:return r.structuredContent
    return json.loads(r.content[0].text)
async def main():
    sid=None
    async with stdio_client(StdioServerParameters(command=str(ROOT/'UniversalDesktopBridge.exe'),
                          args=['--stdio','--home',str(ROOT)])) as (read,write):
        async with ClientSession(read,write) as m:
            await m.initialize()
            proposal=await call(m,'propose_desktop_session',{'project':'demo',
                'task':'Read-only Windows guest process/session/desktop access diagnostics; no mouse or keyboard injection',
                'duration_seconds':210})
            rid=proposal['request_id']
            try:
                for _ in range(30):
                    info=await call(m,'get_request_status',{'request_id':rid})
                    if info['status']=='started':
                        sid=info['result']['session_id'];break
                    if info['status'] in ('failed','rejected','expired'):
                        raise RuntimeError('No start: '+str(info)[:300])
                    await asyncio.sleep(2)
                if sid is None:raise TimeoutError('Startup not scheduled')
                for _ in range(35):
                    status=await call(m,'desktop_status',{'session_id':sid})
                    if status.get('ready'):
                        state=status['guest_state_untrusted']
                        print('GUEST_DIAG_JSON='+json.dumps(state.get('guest_diagnostics'),ensure_ascii=False),flush=True)
                        print('GUEST_ACTIVE_TITLE='+repr(state.get('active_title')),flush=True)
                        await call(m,'desktop_action',{'session_id':sid,'action':'open_demo'})
                        print('SANDBOX_READY_FOR_WINAPP='+sid,flush=True)
                        await asyncio.sleep(180)
                        return
                    if status.get('status')!='running':raise RuntimeError('Sandbox stopped')
                    await asyncio.sleep(3)
                raise TimeoutError('Guest not ready')
            finally:
                if sid:
                    stopped=await call(m,'desktop_stop',{'session_id':sid})
                    print('STOP='+json.dumps(stopped,ensure_ascii=False)[:250],flush=True)
if __name__=='__main__':
    asyncio.run(main())
