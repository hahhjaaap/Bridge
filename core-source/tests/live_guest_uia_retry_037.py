"""Real, isolated Windows Sandbox acceptance for demo-only UI Automation and AI cursor."""
import asyncio,base64,json,sys,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
ROOT=Path(__file__).resolve().parents[1]
SID=None
async def call(client,tool,args,save=None):
    r=await client.call_tool(tool,args)
    blocks=[]
    for block in r.content:
        if block.type=='text':
            try: blocks.append(json.loads(block.text))
            except ValueError: blocks.append(block.text)
        elif block.type=='image':
            if save:
                data=base64.b64decode(block.data,validate=True)
                Path(save).write_bytes(data)
                blocks.append({'image_saved':str(save),'size':len(data)})
    result=r.structuredContent if r.structuredContent else (blocks[0] if blocks else {})
    print(tool+'='+json.dumps(result,ensure_ascii=False,default=str)[:850],flush=True)
    if r.isError: raise RuntimeError(str(result)[:600])
    return result
async def main():
    sid=None
    async with stdio_client(StdioServerParameters(command=str(ROOT/'UniversalDesktopBridge.exe'),
                  args=['--stdio','--home',str(ROOT)])) as (read,write):
        async with ClientSession(read,write) as client:
            await client.initialize()
            req=await call(client,'propose_desktop_session',{'project':'demo',
                'task':'Acceptance: use guest-only Windows UI Automation to fill Sandbox Pad, press Confirm, and show visible AI pointer. No host desktop interaction.',
                'duration_seconds':480})
            try:
                for i in range(60):
                    r=await call(client,'get_request_status',{'request_id':req['request_id']}) if i%5==0 else await silent(client,'get_request_status',{'request_id':req['request_id']})
                    if r['status']=='started':sid=r['result']['session_id'];break
                    if r['status'] in ('failed','rejected','expired'):raise RuntimeError(str(r))
                    await asyncio.sleep(1)
                if not sid: raise RuntimeError('Sandbox start timed out')
                for i in range(90):
                    state=await silent(client,'desktop_status',{'session_id':sid})
                    if state.get('ready'):break
                    if state['status']!='running':raise RuntimeError(str(state))
                    await asyncio.sleep(1)
                if not state.get('ready'):raise RuntimeError('Guest was not ready')
                print('SANDBOX_ID='+sid,flush=True)
                await call(client,'desktop_action',{'session_id':sid,'action':'open_demo'})
                await asyncio.sleep(3)
                def output(name):return ROOT/'state'/'desktops'/sid/'output'/name
                print('OVERLAY_ERROR='+repr(output('overlay-error.txt').read_text('utf8')[:500]) if output('overlay-error.txt').is_file() else 'OVERLAY_ERROR=NONE',flush=True)
                try:
                    await call(client,'desktop_screenshot',{'session_id':sid},save=ROOT/'assets'/'sandbox-desktop-acceptance'/'uia037-before-input.png')
                except Exception as e:
                    print('PRE_CAPTURE_ERROR='+str(e)[:350],flush=True)
                await asyncio.sleep(3)
                await call(client,'desktop_action',{'session_id':sid,'action':'state'})
                # Type directly via Win32 input with UIAutomation fallback.
                try:await call(client,'desktop_action',{'session_id':sid,'action':'type','text':'UDB_UIA_AND_CURSOR_PASS'})
                except Exception as e:print('TYPE_TEST_ERROR='+str(e)[:550],flush=True)
                await asyncio.sleep(1)
                try:await call(client,'desktop_action',{'session_id':sid,'action':'click','x':500,'y':463})
                except Exception as e:print('CLICK_TEST_ERROR='+str(e)[:550],flush=True)
                await asyncio.sleep(2)
                file=output('demo-result.txt')
                print('CONFIRM_PROOF='+repr(file.read_text('utf8')) if file.is_file() else 'CONFIRM_PROOF=NOT_CREATED',flush=True)
                if output('cursor.json').is_file():
                    print('AI_CURSOR_STATE='+output('cursor.json').read_text('utf8'),flush=True)
                else:
                    print('AI_CURSOR_STATE=MISSING',flush=True)
                try:
                    await call(client,'desktop_screenshot',{'session_id':sid},save=ROOT/'assets'/'sandbox-desktop-acceptance'/'uia037-live.png')
                    print('SCREENSHOT_OK',flush=True)
                except Exception as e: print('SCREENSHOT_FAIL='+str(e)[:500],flush=True)
                print('ACCEPTANCE='+('PASS' if file.is_file() and file.read_text('utf8')=='UDB_UIA_AND_CURSOR_PASS' else 'INCOMPLETE'),flush=True)
            finally:
                if sid:
                    try: await call(client,'desktop_stop',{'session_id':sid})
                    except Exception as e:print('STOP_ERROR='+str(e)[:300],flush=True)
async def silent(client,tool,args):
    r=await client.call_tool(tool,args)
    if r.isError:raise RuntimeError(str(r.content)[:400])
    if r.structuredContent:return r.structuredContent
    return json.loads(r.content[0].text)
if __name__=='__main__':asyncio.run(main())
