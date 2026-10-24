"""Real MCP client handshake and tool calls. Does NOT certify ChatGPT Chat."""
import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client

async def check(session):
    await session.initialize()
    tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    assert len(names) == 35, names
    assert {'propose_desktop_session','desktop_action','desktop_screenshot','desktop_status','desktop_stop','propose_media_download',
            'desktop_snapshot','desktop_wait_for','desktop_element_action','host_status','host_start_session',
            'host_browser','host_workspace','host_screenshot','host_close_session'} <= names
    assert not any('approve' in n or 'delete' in n or 'shell' in n for n in names)
    for name, arguments in [('bridge_status', {}), ('list_projects', {}),
         ('read_file', {'project':'demo','path':'hello.py'}),
         ('check_python_syntax', {'project':'demo','path':'hello.py'})]:
        result = await session.call_tool(name, arguments)
        assert not result.isError, result
        content = result.structuredContent or [getattr(b, 'text', '') for b in result.content]
        print(json.dumps({'tool': name, 'structured':content}, ensure_ascii=False))
    blocked = await session.call_tool('read_file', {'project':'demo','path':'../state/agent.sqlite3'})
    assert blocked.isError
    host = await session.call_tool('host_status', {})
    assert not host.isError
    disabled = await session.call_tool('host_start_session', {'kind':'browser','task':'must not start without the local host worker'})
    assert disabled.isError
    print(json.dumps({'protocol':'MCP', 'tool_count':len(names), 'path_escape_blocked':True,
                      'chatgpt_chat_verified_by_this_test':False}))

async def main():
    p = argparse.ArgumentParser()
    p.add_argument('--exe')
    p.add_argument('--http', action='store_true')
    p.add_argument('--isolated', action='store_true', help='Disposable home and demo source; never reads live state')
    args = p.parse_args()
    source_home = Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(source_home))
    temporary = tempfile.TemporaryDirectory(prefix='udb-mcp-') if args.isolated else None
    home = Path(temporary.name)/'bridge' if temporary else source_home
    if temporary:
        if args.http:
            raise ValueError('Isolated mode uses stdio; HTTP needs its own dedicated server')
        from udb.core import Agent
        source = Path(temporary.name)/'source'
        source.mkdir()
        (source/'hello.py').write_text('value = 1\n',encoding='utf-8')
        Agent(home).register_local('demo',str(source),True)
    if args.http:
        token = (home/'state'/'http-token').read_text().strip()
        async with streamablehttp_client('http://127.0.0.1:17843/mcp', headers={'Authorization':'Bearer '+token}) as (r,w,_):
            async with ClientSession(r,w) as session:
                await check(session)
    else:
        cmd = args.exe or sys.executable
        argv = [] if args.exe else [str(source_home/'bridge.py')]
        argv += ['--stdio','--home',str(home)]
        async with stdio_client(StdioServerParameters(command=cmd,args=argv)) as (r,w):
            async with ClientSession(r,w) as session:
                await check(session)
    if temporary:
        temporary.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
