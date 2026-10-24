"""Native fixed-fixture QA and real MCP default-denial, without issuing an owner grant."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client


async def acceptance(exe):
    if os.name!='nt':raise RuntimeError('Windows required')
    exe=str(Path(exe).resolve())
    with tempfile.TemporaryDirectory(prefix='udb-computerqa-') as temp:
        home=Path(temp)/'agent';home.mkdir();(home/'QA-ISOLATED').write_text('fixed synthetic fixture')
        native=subprocess.run([exe,'--computer-primitives-qa','--home',str(home)],capture_output=True,text=True,
            encoding='utf-8',errors='replace',timeout=50,creationflags=subprocess.CREATE_NO_WINDOW)
        if native.returncode:raise RuntimeError('Native computer fixture failed: '+native.stderr[-4000:]+native.stdout[-2000:])
        result=json.loads(native.stdout.strip().splitlines()[-1]);assert result['computer_primitives']=='PASS'
        # An explicitly redirected negative test must fail before any permission is issued.
        # PyInstaller's bootloader has a child; own both in a Job so even a
        # regression/timeout cannot leave a consent prompt running behind QA.
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from udb.computer_jobs import CommandProcess
        rejected=CommandProcess([exe,'--owner-computer-access','--home',str(home)],home,home/'redirected-grant.log')
        try:
            until=time.monotonic()+20
            while rejected.poll() is None and time.monotonic()<until:time.sleep(.1)
            assert rejected.poll() is not None,'Redirected owner activation did not exit'
            assert rejected.poll()!=0 and not (home/'state'/'computer-access.dpapi').exists()
        finally:rejected.close()
        with (home/'agent.log').open('wb') as output:
            process=subprocess.Popen([exe,'--headless','--no-connect','--home',str(home)],
                stdin=subprocess.DEVNULL,stdout=output,stderr=output,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            until=time.monotonic()+20
            while not (home/'state'/'host'/'status.json').exists():
                assert process.poll() is None
                if time.monotonic()>until:raise TimeoutError('Headless worker did not start')
                await asyncio.sleep(.1)
            probe=CommandProcess([exe,'--probe','--home',str(home)],home,home/'probe-default.log')
            try:
                deadline=time.monotonic()+35
                while probe.poll() is None and time.monotonic()<deadline:await asyncio.sleep(.1)
                assert probe.poll()==2,'Default probe must exit with owner-access needed'
                probe_result=json.loads((home/'probe-default.log').read_text('utf-8').strip())
                assert probe_result['result']=='NEEDS_OWNER_ACCESS'
                assert probe_result['permission_created'] is False
                assert not (home/'state'/'computer-access.dpapi').exists()
            finally:probe.close()
            async with stdio_client(StdioServerParameters(command=exe,args=['--stdio','--home',str(home)])) as (r,w):
                async with ClientSession(r,w) as session:
                    await session.initialize()
                    names={t.name for t in (await session.list_tools()).tools}
                    assert len(names)==35
                    assert {'computer_status','computer_files','computer_execute','computer_process','computer_desktop'}<=names
                    status=await session.call_tool('computer_status',{})
                    assert not status.isError
                    value=status.structuredContent or json.loads(status.content[0].text)
                    assert value['enabled'] is False
                    for name,args in [('computer_files',dict(action='write',path=str(home/'projects'/'qa'/'must-not-exist'),content='NO',expected_sha256='NEW')),
                        ('computer_execute',dict(program='powershell.exe',arguments=['-Command','Write-Output forbidden'],cwd=str(home/'projects'/'qa'))),
                        ('computer_process',dict(action='list')),('computer_desktop',dict(action='list'))]:
                        denied=await session.call_tool(name,args)
                        assert denied.isError,(name,denied)
                    assert not (home/'projects'/'qa'/'must-not-exist').exists()
            result.update(mcp_tool_count=35,default_denied=True,redirected_grant_denied=True,
                          real_owner_grant_tested=False,chatgpt_new_tool_discovery_verified=False,
                          default_probe_never_grants=True)
            print(json.dumps(result))
        finally:
            record=json.loads((home/'state'/'headless-status.json').read_text())
            (home/'state'/'headless-stop.json').write_text(json.dumps(dict(instance=record['instance'],expires=time.time()+30)))
            try:process.wait(timeout=15)
            except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--exe',required=True)
    asyncio.run(acceptance(p.parse_args().exe))
