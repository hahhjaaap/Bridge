"""Fixed local-MCP probe: failures/cleanup and no grant or external tool paths."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace as NS
import pytest
from udb import probe


class Session:
    def __init__(self, enabled=True, fail=None, busy=False):
        self.enabled=enabled;self.fail=fail;self.busy=busy;self.calls=[];self.sid='a'*32
    async def list_tools(self):return NS(tools=[NS(name=n) for n in probe.REQUIRED])
    async def call_tool(self,name,args):
        self.calls.append((name,args))
        if name==self.fail:return NS(isError=True,structuredContent=None,content=[NS(type='text',text='SECRET SERVER ERROR')])
        values={
            'bridge_status':dict(version='test',manager=dict(mode='headless'),host_background=dict(worker_ready=True)),
            'computer_status':dict(enabled=self.enabled),
            'computer_execute':dict(job_id='job'),
            'computer_process':dict(status='exited',exit_code=0,output='UDB_COMMAND_OK'),
            'host_status':dict(enabled=True,sessions=[dict(kind='workspace')] if self.busy else []),
            'host_start_session':dict(session_id=self.sid),
            'host_workspace':dict(text=probe.TEXT),
            'host_close_session':dict(closed=True)}
        if name=='computer_files':
            v=dict(sha256='fixture-sha')
            if args['action']=='read':v['content']='UDB_COMMAND_OK' if Path(args['path']).name=='command.txt' else probe.TEXT
        elif name=='computer_desktop':
            action=args['action']
            v={'list':dict(windows=[dict(title='User window must never be selected',window_id='foreign'),
                    dict(title='UDB Workspace · '+self.sid[:8],window_id='owned')]),
               'snapshot':dict(elements=[dict(automation_id='1001')]),
               'set_value':dict(verified_control_value=True)}[action]
        else:v=values[name]
        # Exercise the JSON text fallback used by computer_desktop.
        return NS(isError=False,structuredContent=None,content=[NS(type='text',text=json.dumps(v))])


def exercise(session,tmp_path):
    report=dict(chatgpt_tool_discovery_verified=False)
    asyncio.run(probe.exercise(session,tmp_path,tmp_path/'powershell.exe',report))
    return report


def test_probe_real_protocol_shape_and_exact_owned_window(tmp_path):
    session=Session();report=exercise(session,tmp_path)
    assert report['result']=='PASS' and report['cleanup']=='PASS'
    assert not report['chatgpt_tool_discovery_verified']
    assert all(args.get('window_id')!='foreign' for _,args in session.calls)
    executed=next(args for name,args in session.calls if name=='computer_execute')
    assert executed['timeout_seconds']==30 and executed['arguments'][:2]==['-NoProfile','-NonInteractive']
    assert [name for name,_ in session.calls][-2:]==['computer_process','host_close_session']


def test_probe_without_owner_grant_never_requests_file_process_or_desktop(tmp_path):
    session=Session(enabled=False);report=exercise(session,tmp_path)
    assert report['result']=='NEEDS_OWNER_ACCESS'
    assert [name for name,_ in session.calls]==['bridge_status','computer_status']


def test_probe_does_not_take_over_or_close_existing_workspace(tmp_path):
    session=Session(busy=True);report=exercise(session,tmp_path)
    assert report['result']=='PARTIAL'
    assert report['uia_roundtrip']=='SKIPPED_EXISTING_WORKSPACE'
    assert not any(name in ('host_start_session','host_close_session','computer_desktop') for name,_ in session.calls)


@pytest.mark.parametrize('failure',['computer_process','computer_desktop','host_workspace'])
def test_probe_failure_stops_only_its_resources_and_never_replays(tmp_path,failure):
    session=Session(fail=failure);report={}
    with pytest.raises(probe.ProbeFailure) as exc:
        asyncio.run(probe.exercise(session,tmp_path,tmp_path/'powershell.exe',report))
    assert 'SECRET' not in str(exc.value)
    assert sum(name=='computer_execute' for name,_ in session.calls)==1
    assert any(name=='computer_process' and args.get('action')=='stop' and args.get('job_id')=='job' for name,args in session.calls)
    if failure!='computer_process':
        assert session.calls[-1]==('host_close_session',dict(session_id=session.sid))


def test_payload_rejects_unstructured_results_without_echoing_content():
    with pytest.raises(probe.ProbeFailure,match='invalid structured data'):
        probe.payload(NS(isError=False,structuredContent=None,content=[NS(type='text',text='SECRET')]),'fixture')
