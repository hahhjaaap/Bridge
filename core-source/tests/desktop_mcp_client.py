"""Manual acceptance client: MCP calls only; never approves a local request."""
import argparse
import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    sys.stdout.reconfigure(encoding='utf-8',errors='replace')
    p=argparse.ArgumentParser();p.add_argument('tool',choices=['propose_desktop_session','get_request_status','desktop_status','desktop_action','desktop_screenshot','desktop_stop','sequence'])
    p.add_argument('--args-file',required=True);p.add_argument('--exe',required=True);p.add_argument('--image');p.add_argument('--source',action='store_true')
    a=p.parse_args();home=Path(__file__).resolve().parents[1]
    params=StdioServerParameters(command=sys.executable if a.source else a.exe,args=([str(home/'bridge.py')] if a.source else [])+['--stdio','--home',str(home)])
    async with stdio_client(params) as (r,w):
        async with ClientSession(r,w) as s:
            await s.initialize();arguments=json.loads(Path(a.args_file).read_text('utf-8-sig'))
            sequence=arguments if a.tool=='sequence' else [{'tool':a.tool,'arguments':arguments}]
            for step in sequence:
                result=await s.call_tool(step['tool'],step['arguments'])
                print(json.dumps({'tool':step['tool']}),flush=True)
                for block in result.content:
                    if block.type=='text':print(block.text,flush=True)
                    elif block.type=='image':
                        data=base64.b64decode(block.data,validate=True)
                        target=step.get('image',a.image)
                        if target:Path(target).write_bytes(data)
                        print(json.dumps({'image_bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'saved':target}),flush=True)
                if result.isError:raise SystemExit(1)
if __name__=='__main__':asyncio.run(main())
