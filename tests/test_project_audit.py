"""Failure-path and policy regressions found during the full project audit."""
import json
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
import pytest
from udb import cli, owner_command
from udb.config import activate, checked, effective, load
from udb.companion.background import Background
from udb.companion.runtime import Runtime, ScopedFiles, digest
from udb.core import Agent, PolicyError
from udb import host_client


def symlink_or_skip(link,target,directory=False):
    try:link.symlink_to(target,target_is_directory=directory)
    except OSError:pytest.skip('Symlink permission unavailable')


def test_revocation_command_remains_available_without_executing_it(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(owner_command,'main',lambda argv:calls.append(argv) or 0)
    assert cli.main(['revoke-access','--home',str(tmp_path)])==0
    assert calls[0][0]=='revoke-access' and '--access' not in calls[0]
    assert not (tmp_path/'state').exists()


def test_explicit_missing_configuration_is_not_silently_ignored(tmp_path):
    with pytest.raises(ValueError,match='configuration.*(missing|exist)|Configuration.*(missing|exist)'):
        load(config=tmp_path/'mistyped.toml')


def test_effective_config_survives_equivalent_home_paths(tmp_path):
    home=tmp_path/'home';home.mkdir();(home/'child').mkdir()
    config=tmp_path/'settings.toml'
    config.write_text('[bridge]\nhome="home/child/.."\nproject_root="actual-project"\nenable_terminal=true\n')
    selected=load(config=config);old=activate(selected)
    try:
        for alias in (home.resolve(),home/'child/..'):
            actual=effective(alias)
            assert actual.enable_terminal is True
            assert actual.project_root==(tmp_path/'actual-project').resolve()
        assert selected.home==home.resolve()
    finally:activate(old)


def test_explicit_config_does_not_allow_a_redirected_default_home(tmp_path,monkeypatch):
    target=tmp_path/'real-home';target.mkdir()
    link=tmp_path/'alias';symlink_or_skip(link,target,True)
    monkeypatch.setenv('UDB_HOME',str(link))
    cfg=tmp_path/'settings.toml';cfg.write_text('[bridge]\nenable_terminal=false\n')
    with pytest.raises(ValueError,match='Linked'):load(config=cfg)


def test_init_persists_the_requested_settings_and_selected_file(tmp_path):
    config=tmp_path/'settings.toml';home=tmp_path/'owner-home';root=tmp_path/'source'
    assert cli.main(['init','--config',str(config),'--home',str(home),'--root',str(root),'--enable-terminal'])==0
    assert config.is_file() and not (home/'udb.toml').exists()
    actual=load(config=config)
    assert actual.home==home.resolve() and actual.project_root==root.resolve() and actual.enable_terminal
    assert not (home/'state').exists()
    assert cli.main(['init','--config',str(config)])!=0
    assert load(config=config)==actual


@pytest.mark.parametrize('phase',['lease','save','before_start','thread_start'])
def test_failed_job_setup_releases_capacity_and_lease(tmp_path,monkeypatch,phase):
    bg=Background(ScopedFiles(str(tmp_path)));keys=[];ran=[]
    original=bg.journal.lease
    def lease(key):
        keys.append(key)
        if phase=='lease':raise OSError('fixture lease failure')
        return original(key)
    def fail(*a,**kw):raise OSError('fixture setup failure')
    with monkeypatch.context() as m:
        m.setattr(bg.journal,'lease',lease)
        if phase=='save':m.setattr(bg.journal,'save',fail)
        if phase=='thread_start':m.setattr('udb.companion.background.Task.start',fail)
        with pytest.raises(OSError):bg.new('fixture',lambda t:ran.append(True),before_start=fail if phase=='before_start' else None)
    assert not ran and not bg.tasks
    for key in keys:
        handle=bg.journal.lease(key);handle.close()
    assert not bg.journal.records('job')
    row=bg.new('fixture',lambda t:{'ok':True})
    bg.tasks[row['job_id']].thread.join(3)
    assert bg.task(row['job_id'])['state']=='completed'
    bg.close()


def test_reloaded_jobs_page_results_and_distinguish_live_ownership(tmp_path):
    bg=Background(ScopedFiles(str(tmp_path)));jid='a'*32;key='job-'+jid
    rows=[{'n':i} for i in range(230)]
    bg.journal.save(key,dict(job_id=jid,state='running',results=rows,result={},committed=False))
    lease=bg.journal.lease(key)
    try:
        live=bg.task(jid,offset=100,limit=17)
        assert live['state']=='owned_by_live_backend'
        assert live['results']==rows[100:117] and live['next_offset']==117 and live['has_more']
    finally:lease.close()
    lost=bg.task(jid,offset=220,limit=17)
    assert lost['state']=='interrupted' and lost['outcome_unknown']
    assert lost['results']==rows[220:] and not lost['has_more'] and lost['next_offset']==230
    with pytest.raises(ValueError):bg.task(jid,offset=-1)


@pytest.mark.parametrize('ending',['\n','\r\n'])
def test_large_text_read_returns_every_byte_before_next_offset(tmp_path,ending):
    original='abcd'*25000+ending+' \t'+ending
    (tmp_path/'large.txt').write_bytes(original.encode('utf-8'))
    rt=Runtime(str(tmp_path))
    try:
        row=rt.tool('file_read',dict(path='large.txt',limit=len(original)))
        text=row['content'][0]['text']
        body=text.split('\n',2)[2]
        assert body==original
        assert row['structuredContent']['content']==original
        assert 'Next offset: '+str(len(original)) in text
    finally:rt.close()


def test_guarded_write_refuses_redirected_recovery_directory(tmp_path):
    project=tmp_path/'project';outside=tmp_path/'outside';project.mkdir();outside.mkdir()
    path=project/'source.txt';path.write_bytes(b'original')
    symlink_or_skip(project/'.udb-recovery',outside,True)
    files=ScopedFiles(str(project))
    with pytest.raises((PermissionError,ValueError)):files.write('source.txt','replacement',digest(b'original'))
    assert path.read_bytes()==b'original' and not list(outside.iterdir())


def test_terminal_with_closed_output_still_accepts_live_stdin(tmp_path):
    rt=Runtime(str(tmp_path),True)
    script="import os,sys;from pathlib import Path;os.close(1);os.close(2);Path('ready').write_text('ready');Path('answer').write_text(sys.stdin.readline(),encoding='utf-8')"
    try:
        started=rt.start(sys.executable,['-u','-c',script],wait_ms=0)
        session=rt.sessions[started['session_id']]
        deadline=time.monotonic()+5
        while not session.done or not (tmp_path/'ready').exists():
            assert time.monotonic()<deadline
            time.sleep(.02)
        assert session.process.poll() is None
        assert not session.input_done.is_set()
        rt.checkpoint()
        assert rt.journal.load('terminal-'+session.id)['state']!='finished'
        result=rt.tool('terminal_send',dict(session_id=session.id,text='Còn stdin 🚀\n'))
        assert not result.get('isError'),result
        assert session.process.wait(timeout=5)==0
        assert (tmp_path/'answer').read_text(encoding='utf-8')=='Còn stdin 🚀\n'
    finally:rt.close()


def test_terminal_started_during_journal_failure_keeps_its_session_id(tmp_path,monkeypatch):
    rt=Runtime(str(tmp_path),True)
    try:
        with monkeypatch.context() as m:
            def fail():raise OSError('fixture disk full')
            m.setattr(rt,'checkpoint',fail)
            row=rt.tool('terminal_start',dict(program=sys.executable,arguments=['-u','-c','import time;time.sleep(30)'],wait_ms=0))
        assert not row.get('isError'),row
        data=row['structuredContent']
        assert data['session_id'] in rt.sessions and data['persistence_available'] is False
        assert 'do not replay' in row['content'][0]['text'].lower()
        assert len(rt.sessions)==1
    finally:rt.close()


@pytest.mark.parametrize('value',[[],None,True,{'enabled':True,'resources':[]}, {'worker_ready':True,'resources':{'at':time.time()+3600}}, {'worker_ready':True,'resources':{'at':True}}])
def test_malformed_or_future_host_health_fails_closed(tmp_path,value):
    state=tmp_path/'state';(state/'host').mkdir(parents=True)
    (state/'host-settings.json').write_text(json.dumps(value))
    (state/'host/status.json').write_text(json.dumps(value))
    agent=SimpleNamespace(state=state)
    report=host_client.status(agent)
    assert report['worker_ready'] is False and report['resources']['admission_ready'] is False
    assert isinstance(host_client.enabled(agent),bool)


@pytest.mark.parametrize('redirect',['state','database'])
def test_agent_refuses_redirected_private_storage(tmp_path,redirect):
    home=tmp_path/'home';outside=tmp_path/'outside';home.mkdir();outside.mkdir()
    if redirect=='state':symlink_or_skip(home/'state',outside,True)
    else:
        state=home/'state';state.mkdir();target=outside/'db';target.write_bytes(b'')
        symlink_or_skip(state/'agent.sqlite3',target)
    with pytest.raises(PolicyError):Agent(home)
    if redirect=='state':assert not list(outside.iterdir())
    else:assert (outside/'db').read_bytes()==b''


@pytest.mark.parametrize('relative',['.env.local','.env.production','credentials.dev.json','secrets.test.json','.ssh/key.txt','training_data/example.txt','training-data/example.txt','weights/example.txt','models/example.txt','dataset/example.txt'])
def test_companion_hides_common_secret_and_training_paths(tmp_path,relative):
    p=tmp_path/relative;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('synthetic fixture, no real credential')
    files=ScopedFiles(str(tmp_path))
    with pytest.raises(PermissionError):files.read(relative)
    with pytest.raises(PermissionError):files.write(relative,'changed',digest(p.read_bytes()))
    assert relative not in files.search(text='synthetic')['matches']


def test_process_protection_uses_bridge_home_even_for_external_projects(tmp_path,monkeypatch):
    from udb.companion.system_processes import ProcessCatalog
    home=tmp_path/'bridge';root=tmp_path/'external-project';root.mkdir()
    catalog=ProcessCatalog(root,bridge_home=home)
    def rows():return [
        dict(pid=424242,parent_pid=1,name='UDBHost.exe',creation=99,same_user=True,critical=False,image=str(home/'vendor/UDBHost.exe'),cpu_seconds=0),
        dict(pid=424243,parent_pid=1,name='editor.exe',creation=100,same_user=True,critical=False,image=str(tmp_path/'editor.exe'),cpu_seconds=0)]
    monkeypatch.setattr(catalog,'_windows',rows);monkeypatch.setattr(catalog,'_linux',rows)
    data=catalog.list()['processes']
    protected=next(r for r in data if r['pid']==424242)
    assert not protected['can_terminate'] and 'observation_id' not in protected
    assert next(r for r in data if r['pid']==424243)['can_terminate']


def test_http_auth_token_refuses_linked_private_file(tmp_path):
    from udb.server import token_for
    state=tmp_path/'state';state.mkdir();target=tmp_path/'sentinel';target.write_text('synthetic-only')
    symlink_or_skip(state/'http-token',target)
    with pytest.raises(PolicyError):token_for(SimpleNamespace(state=state))
    assert target.read_text()=='synthetic-only'


@pytest.mark.parametrize('update',[{'id':True},{'id':[]},{'id':{}},{'id':1.5},{'jsonrpc':'1.0'},{'method':[]},{'method':None}])
def test_malformed_rpc_request_never_reaches_dispatch(monkeypatch,update):
    import io,threading
    from udb.proxy import UnifiedProxy
    request=dict(jsonrpc='2.0',id=9,method='tools/call',params=dict(name='would_mutate',arguments={}))
    request.update(update);dispatched=[];replies=[]
    server=UnifiedProxy.__new__(UnifiedProxy)
    server.active_limit=threading.Semaphore(8)
    server.primary=server.companion=SimpleNamespace(notification=lambda m:dispatched.append(m))
    server.send=replies.append
    def handle(row):dispatched.append(row);server.active_limit.release()
    server.handle=handle
    monkeypatch.setattr('udb.proxy.sys.stdin',SimpleNamespace(buffer=io.BytesIO((json.dumps(request)+'\n').encode())))
    server.serve()
    for _ in range(8):assert server.active_limit.acquire(timeout=2)
    assert not dispatched
    assert len(replies)==1 and replies[0]['error']['code']==-32600
