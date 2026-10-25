"""Canonical configuration/registry/CLI and an actual source-only MCP tree."""
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest
from udb import __version__
from udb.config import load
from udb.launcher import command,worker_command,child_environment
from udb.protocol import annotate
ROOT=Path(__file__).resolve().parents[1]

def run(*args,cwd=None):return subprocess.run(command(args),cwd=cwd or ROOT,capture_output=True,text=True,timeout=40,env=child_environment())

def test_single_version_and_generated_registry(tmp_path):
    value=run('--version',cwd=tmp_path);assert value.returncode==0 and value.stdout.strip()==__version__
    result=run('tools',cwd=tmp_path);assert result.returncode==0,result.stderr
    data=json.loads(result.stdout);names={t['name'] for t in data['tools']}
    assert len(names)==data['tool_count']==60
    assert data['backend_count']==data['core_count']+data['companion_count']==66
    assert {'virtual_pointer','work_recovery','terminal_start','computer_desktop','spreadsheet'}<=names

def test_configuration_precedence_relative_paths_and_no_grants(tmp_path):
    cfg=tmp_path/'settings.toml';cfg.write_text('[bridge]\nhome="home"\nproject_root="project"\nenable_terminal=true\n')
    value=load(config=cfg);assert value.home==tmp_path/'home' and value.project_root==tmp_path/'project' and value.enable_terminal
    value=load(config=cfg,root=tmp_path/'other',enable_terminal=False);assert value.project_root==tmp_path/'other' and not value.enable_terminal
    assert not (tmp_path/'home').exists()
    cfg.write_text('[bridge]\napi_key="not-a-key-do-not-echo"\n')
    with pytest.raises(ValueError,match='credentials'):load(config=cfg)
    result=run('status','--config',str(cfg),'--json');assert 'not-a-key-do-not-echo' not in result.stderr+result.stdout

def test_config_rejects_links_and_nonboolean_execution(tmp_path):
    cfg=tmp_path/'settings.toml';cfg.write_text('[bridge]\nenable_terminal="true"\n')
    with pytest.raises(ValueError,match='boolean'):load(config=cfg)
    link=tmp_path/'link.toml'
    try:link.symlink_to(cfg)
    except OSError:pytest.skip('Symlinks unavailable')
    with pytest.raises(ValueError,match='Linked'):load(config=link)

def test_old_modules_are_aliases_not_duplicate_implementations():
    for old,new in [('udb_companion','udb.companion.runtime'),('udb_unified','udb.proxy'),('udb_features','udb.companion.features'),('udb_managed_process','udb.companion.processes')]:
        assert importlib.import_module(old) is importlib.import_module(new)

def test_worker_launch_and_environment_never_require_external_scripts(monkeypatch):
    monkeypatch.setenv('CONTROL_PLANE_API_KEY','private-sentinel');monkeypatch.setenv('TUNNEL_CLIENT_PROFILE_DIR','private-profile')
    env=child_environment();assert 'CONTROL_PLANE_API_KEY' not in env and 'TUNNEL_CLIENT_PROFILE_DIR' not in env
    assert worker_command('document')[1:]==['-m','udb','_worker','document']
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    assert worker_command('core',['--stdio'])==[sys.executable,'_worker','core','--stdio']
    assert child_environment()['PYINSTALLER_RESET_ENVIRONMENT']=='1'

def test_owner_activation_not_accepted_by_mcp_serve(tmp_path):
    result=run('--access','computer','serve','--home',str(tmp_path),'--root',str(tmp_path))
    assert result.returncode!=0 and 'Owner activation' in result.stderr
    assert not (tmp_path/'state').exists()

def test_status_metadata_preserves_legacy_payload():
    old={'content':[{'type':'text','text':'RUNNING | started'}],'structuredContent':{'job_id':'owned','state':'running'}}
    new=annotate(old);assert new['content']==old['content'] and new['structuredContent']==old['structuredContent']
    assert new['_meta']['udb']['status']=='RUNNING' and new['_meta']['udb']['replay_performed'] is False
    assert annotate(dict(old,isError=True))['_meta']['udb']['status']=='ERROR'

def test_real_source_mcp_without_installed_core_or_owner_access(tmp_path):
    import queue,threading
    home=tmp_path/'home';root=tmp_path/'project';home.mkdir();root.mkdir();rows=queue.Queue()
    p=subprocess.Popen(command(['serve','--home',str(home),'--root',str(root)]),cwd=tmp_path,env=child_environment(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    def receive():
        for line in p.stdout:
            try:rows.put(json.loads(line))
            except ValueError:pass
    thread=threading.Thread(target=receive,daemon=True);thread.start()
    seq=0
    def rpc(method,params,allow_error=False):
        nonlocal seq
        seq+=1;p.stdin.write(json.dumps(dict(jsonrpc='2.0',id=seq,method=method,params=params))+'\n');p.stdin.flush()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            reply=rows.get(timeout=max(.01,deadline-time.monotonic()))
            if reply.get('id')==seq:
                if allow_error and 'error' in reply:return reply
                assert 'error' not in reply,reply
                return reply['result']
        raise AssertionError('MCP timeout')
    try:
        rpc('initialize',dict(protocolVersion='2024-11-05',capabilities={},clientInfo=dict(name='package-test',version='1')))
        p.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');p.stdin.flush()
        listing=rpc('tools/list',{});assert len(listing['tools'])==60
        status=rpc('tools/call',dict(name='bridge_status',arguments={}));assert not status.get('isError'),status
        assert status['_meta']['udb']['version']==__version__
        denied=rpc('tools/call',dict(name='terminal_start',arguments=dict(program=sys.executable,arguments=['-c','raise SystemExit(19)'])),allow_error=True)
        assert denied.get('isError') or denied.get('error'),denied
        assert denied['error']['data']['udb']['status']=='ERROR'
        assert not (home/'UniversalDesktopBridge.exe').exists()
    finally:
        p.stdin.close()
        try:p.wait(timeout=15)
        except subprocess.TimeoutExpired:p.kill();p.wait(timeout=5)
        p.stdout.close();p.stderr.close();thread.join(timeout=1)

def test_effective_settings_follow_the_manager_without_rewriting_manifest(tmp_path,monkeypatch):
    from udb.config import Settings,activate,manager_options,serve_options
    from udb.desktop import mcp_command
    from udb import cli,owner_command
    home=tmp_path/'home';home.mkdir();root=tmp_path/'scoped-project';root.mkdir()
    settings=Settings(home,root,True)
    previous=activate(settings)
    try:
        assert manager_options(home)==['--runtime-root',str(root),'--runtime-terminal','on']
        assert mcp_command(home)==command(serve_options(home))
        assert str(root) in mcp_command(home) and '--enable-terminal' in mcp_command(home)
    finally:activate(previous)
    cfg=tmp_path/'settings.toml';cfg.write_text('[bridge]\nhome="home"\nproject_root="scoped-project"\nenable_terminal=true\n')
    observed=[]
    def owner(args):observed.append(manager_options(home));return 0
    monkeypatch.setattr(owner_command,'main',owner)
    assert cli.main(['--config',str(cfg),'start'])==0
    assert observed==[['--runtime-root',str(root),'--runtime-terminal','on']]
    assert not (home/'mcp.json').exists()

def test_generated_plugin_manifest_matches_the_source():
    from udb.metadata import manifest
    assert json.loads((ROOT/'plugin.json').read_text())==manifest()
    assert manifest()['version']==__version__

def test_python_pid_requires_live_headless_identity(tmp_path,monkeypatch):
    from udb import owner_command
    (tmp_path/'state').mkdir();(tmp_path/'state/manager.lock').write_text(str(os.getpid())+'\n')
    assert not owner_command.manager_running(tmp_path,Path(sys.executable),image=lambda pid:sys.executable)
    monkeypatch.setattr(owner_command,'headless_state',lambda home:{'pid':os.getpid()})
    assert owner_command.manager_running(tmp_path,Path(sys.executable),image=lambda pid:sys.executable)

def test_build_finds_checkout_when_cli_was_installed_noneditable(monkeypatch):
    from udb import build
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(build,'__file__',str(ROOT/'unrelated-site-packages/udb/build.py'))
    assert build.source_root()==ROOT
