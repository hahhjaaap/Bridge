"""Windows-only acceptance through real MCP and a disposable private engine.

Checks the real browser, Unicode native control, caps, and unchanged foreground
window/cursor. Uses no user profile, account, files or external web service.
Pass --exe for a frozen build. A user moving the mouse during this test can
legitimately invalidate the unchanged-cursor assertion; do not mask that result.
"""
import argparse, asyncio, json, os, subprocess, sys, tempfile, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def acceptance(args):
    if os.name != 'nt': raise RuntimeError('Run the live host acceptance on Windows')
    import win32gui
    root = Path(__file__).resolve().parents[1]
    command = args.exe or sys.executable
    prefix = [] if args.exe else [str(root/'bridge.py')]
    with tempfile.TemporaryDirectory(prefix='udb-hostqa-') as temp:
        home = Path(temp); (home/'QA-ISOLATED').write_text('synthetic acceptance only')
        before = (win32gui.GetForegroundWindow(), win32gui.GetCursorPos())
        with (home/'engine.log').open('wb') as log:
            engine = subprocess.Popen([command,*prefix,'--host-engine-qa','--home',str(home)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            deadline = time.monotonic()+80
            while not (home/'fixture-ready.json').exists():
                if engine.poll() is not None:
                    raise RuntimeError('Isolated host acceptance worker exited: '+(home/'engine.log').read_text('utf-8',errors='replace')[-6000:])
                if time.monotonic() > deadline: raise TimeoutError('Host acceptance fixture not ready (machine may be busy)')
                await asyncio.sleep(.2)
            fixture = json.loads((home/'fixture-ready.json').read_text())
            async with stdio_client(StdioServerParameters(command=command,args=prefix+['--stdio','--home',str(home)])) as (r,w):
                async with ClientSession(r,w) as session:
                    await session.initialize()
                    async def call(name, **kwargs):
                        result = await session.call_tool(name,kwargs)
                        assert not result.isError, result
                        if result.structuredContent: return result.structuredContent
                        return json.loads(result.content[0].text)
                    status = await call('host_status')
                    assert status['host_mouse_keyboard'] is False
                    assert status['existing_user_windows'] is False
                    browser_status=next(x for x in status['sessions'] if x['kind']=='browser')
                    assert browser_status['memory_limit_mib']==1536 and browser_status['cpu_cap_percent']==25
                    sid,tab = fixture['session_id'],fixture['tab_id']
                    text='UDB tiếng Việt — không giành chuột'
                    result=await call('host_browser',session_id=sid,action='fill',tab_id=tab,selector='#name',value=text)
                    assert result['ok']
                    assert (await call('host_browser',session_id=sid,action='click',tab_id=tab,selector='#confirm'))['ok']
                    assert text in (await call('host_browser',session_id=sid,action='read',tab_id=tab))['text']
                    shot=await session.call_tool('host_screenshot',{'session_id':sid,'tab_id':tab})
                    assert not shot.isError and any(getattr(b,'type',None)=='image' for b in shot.content)
                    for _ in range(3):await call('host_browser',session_id=sid,action='new')
                    rejected=await session.call_tool('host_browser',{'session_id':sid,'action':'new'})
                    assert rejected.isError
                    foreign=await session.call_tool('host_browser',{'session_id':sid,'action':'read','tab_id':'user-tab'})
                    assert foreign.isError
                    ws=await call('host_start_session',kind='workspace',task='Disposable Unicode native control',duration_seconds=120)
                    assert (await call('host_workspace',session_id=ws['session_id'],action='set_text',text=text))['ok']
                    assert (await call('host_workspace',session_id=ws['session_id'],action='read'))['text']==text
                    assert len((await call('host_status'))['sessions'])==2
                    assert (await call('host_close_session',session_id=ws['session_id']))['closed']
                    assert (await call('host_close_session',session_id=sid))['closed']
                    assert not (await call('host_status'))['sessions']
            after=(win32gui.GetForegroundWindow(),win32gui.GetCursorPos())
            assert before==after, f'Foreground/cursor changed during live acceptance: before={before}, after={after}; investigate before release'
            print(json.dumps(dict(host_live_acceptance='PASS',foreground_unchanged=True,cursor_unchanged=True,
                own_browser_dom=True,native_unicode=True,tab_cap=True,foreign_tab_denied=True,
                own_cleanup=True,chatgpt_discovery_verified=False)))
        finally:
            (home/'stop.flag').write_text('stop')
            try:engine.wait(timeout=15)
            except subprocess.TimeoutExpired:
                engine.terminate();engine.wait(timeout=5)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--exe');asyncio.run(acceptance(p.parse_args()))
