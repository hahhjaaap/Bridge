"""Real MCP acceptance: isolated Windows Sandbox guest desktop only.
Owner already granted demo project Sandbox autonomy. Does not touch host input.
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / 'UniversalDesktopBridge.exe'
OUT = ROOT / 'assets' / 'sandbox-desktop-acceptance'
OUT.mkdir(parents=True, exist_ok=True)

def report(name, value):
    print(name, json.dumps(value, ensure_ascii=False, default=str)[:2300], flush=True)

async def call(session, tool, args):
    result = await session.call_tool(tool, args)
    objects = []
    for block in result.content:
        if block.type == 'text':
            try: objects.append(json.loads(block.text))
            except ValueError:objects.append(block.text)
        elif block.type == 'image':
            data = base64.b64decode(block.data, validate=True)
            import hashlib
            file = OUT / 'guest-screen.png'
            file.write_bytes(data)
            objects.append({'image':str(file),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    report(tool, objects)
    if result.isError:
        raise RuntimeError(str(objects)[:450])
    if result.structuredContent:
        return result.structuredContent
    return objects[0] if objects else {}

async def main():
    sid = None
    async with stdio_client(StdioServerParameters(
        command=str(EXE), args=['--stdio','--home',str(ROOT)])) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize()
            advertised={tool.name for tool in (await s.list_tools()).tools}
            required={'propose_desktop_session','desktop_status','desktop_screenshot','desktop_action','desktop_stop'}
            assert required<=advertised, (required-advertised)
            report('MCP_DESKTOP_TOOLS',sorted(required))
            try:
                req=await call(s,'propose_desktop_session',{
                    'project':'demo',
                    'task':'Run harmless local Windows Sandbox Pad acceptance; open, screenshot, type a proof phrase in its textbox and click Confirm. No host desktop interaction.',
                    'duration_seconds':360})
                rid=req['request_id']
                deadline=time.monotonic()+150
                while time.monotonic()<deadline:
                    request=await call(s,'get_request_status',{'request_id':rid})
                    result=request.get('result') or {}
                    if request['status']=='started':
                        sid=result['session_id']
                        break
                    if request['status'] in ('failed','expired','rejected'):
                        raise RuntimeError('Desktop launch refused: '+str(request))
                    await asyncio.sleep(3)
                if not sid:raise TimeoutError('Sandbox desktop not started')
                deadline=time.monotonic()+120
                while time.monotonic()<deadline:
                    status=await call(s,'desktop_status',{'session_id':sid})
                    if status.get('ready'):break
                    if status.get('status') not in ('running','reviewing'):raise RuntimeError('VM stopped '+str(status))
                    await asyncio.sleep(4)
                else:raise TimeoutError('Sandbox guest is not ready')
                await call(s,'desktop_action',{'session_id':sid,'action':'open_demo'})
                await asyncio.sleep(3)
                state=await call(s,'desktop_action',{'session_id':sid,'action':'state'})
                if state.get('state'):
                    width=int(state['state']['width']);height=int(state['state']['height'])
                else:
                    width=int(status['guest_state_untrusted']['width']);height=int(status['guest_state_untrusted']['height'])
                report('SCREEN_SIZE',{'width':width,'height':height})
                await call(s,'desktop_screenshot',{'session_id':sid})
                # App 780x480 centered; within the demo textbox at x+50/y+175.
                bx=max(40,(width-780)//2+110)
                by=max(40,(height-480)//2+175)
                await call(s,'desktop_action',{'session_id':sid,'action':'click','x':bx,'y':by})
                await call(s,'desktop_action',{'session_id':sid,'action':'type','text':'UDB_GUEST_KEYBOARD_OK'})
                await call(s,'desktop_action',{'session_id':sid,'action':'click','x':max(40,(width-780)//2+115),'y':max(40,(height-480)//2+300)})
                await asyncio.sleep(1)
                await call(s,'desktop_screenshot',{'session_id':sid})
                demo=ROOT/'state'/'desktops'/sid/'output'/'demo-result.txt'
                if not demo.is_file():raise AssertionError('Guest Confirm did not produce expected output')
                response=demo.read_text('utf8')
                report('SANDBOX_TEXT_PROOF',response)
                assert response=='UDB_GUEST_KEYBOARD_OK',response
                report('ACCEPTANCE','PASS')
            finally:
                if sid:
                    try: await call(s,'desktop_stop',{'session_id':sid})
                    except Exception as exc:report('STOP_ERROR',str(exc))
if __name__=='__main__':
    asyncio.run(main())
