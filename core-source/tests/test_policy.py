import json
import os
import time
from pathlib import Path
import pytest
from udb.core import Agent, PolicyError, digest
from udb.sandbox import build_wsb

@pytest.fixture
def agent(tmp_path):
    a = Agent(tmp_path/'bridge')
    source = tmp_path/'source'
    source.mkdir()
    a.register_local('test', str(source), True)
    (source/'hello.py').write_bytes(b'value = 1\n')
    return a, source

@pytest.mark.parametrize('path', ['../hello.py', '/tmp/a.py', 'C:\\hello.py', 'C:a.py',
    '\\\\server\\share\\a.py', 'hello.py:secret', 'NUL.py', 'COM1.txt', 'a. /x.py',
    'checkpoints/model.py', 'datasets/a.txt', 'data/a.json', 'model.safetensors',
    'train.jsonl', '.env', 'secrets.json', '.git/config', 'state/agent.py',
    'folder/..\\hello.py', 'x.'])
def test_unsafe_paths_blocked(agent, path):
    a, source = agent
    with pytest.raises(PolicyError):
        a.path('test', path, write=True)

def test_protected_root_denies_existing_registration(agent, tmp_path):
    a, source = agent
    a.protect_local(str(source))
    with pytest.raises(PolicyError):
        a.read('test', 'hello.py')

def test_readonly_and_nonregistered(agent, tmp_path):
    a, source = agent
    a.register_local('test', str(source), False)
    assert a.read('test','hello.py')['text'] == 'value = 1\n'
    with pytest.raises(PolicyError):
        a.propose_write('test','hello.py','bad', 'NEW')
    with pytest.raises(PolicyError):
        a.read('missing','hello.py')

def test_write_requires_local_approval_and_backup(agent):
    a, source = agent
    sha = a.read('test','hello.py')['sha256']
    proposal = a.propose_write('test','hello.py','value = 2\n',sha)
    assert (source/'hello.py').read_text() == 'value = 1\n'
    rid = proposal['request_id']
    assert a.request_status(rid)['status'] == 'pending'
    a.decide_local(rid, True)
    assert (source/'hello.py').read_text() == 'value = 2\n'
    assert (a.state/'backups'/(rid+'.bak')).read_text() == 'value = 1\n'
    with pytest.raises(PolicyError):
        a.decide_local(rid, True)

def test_stale_changed_file_no_overwrite(agent):
    a, source = agent
    sha = a.read('test','hello.py')['sha256']
    rid = a.propose_write('test','hello.py','value = 2\n',sha)['request_id']
    (source/'hello.py').write_text('manual edit\n')
    a.decide_local(rid, True)
    assert a.request_status(rid)['status'] == 'failed'
    assert (source/'hello.py').read_text() == 'manual edit\n'

def test_root_rebinding_does_not_apply(agent, tmp_path):
    a, source = agent
    rid = a.propose_write('test','new.py','x = 1\n','NEW')['request_id']
    other = tmp_path/'other'
    other.mkdir()
    a.register_local('test', str(other), True)
    a.decide_local(rid, True)
    assert a.request_status(rid)['status'] == 'failed'
    assert not (other/'new.py').exists()

def test_reject_expire_tamper(agent):
    a, source = agent
    rid = a.propose_write('test','new.py','x=1','NEW')['request_id']
    a.decide_local(rid, False)
    assert not (source/'new.py').exists()
    rid2 = a.propose_write('test','new.py','x=1','NEW')['request_id']
    with a.connect() as c:
        c.execute('UPDATE requests SET created=? WHERE id=?',(time.time()-601,rid2))
    with pytest.raises(PolicyError):
        a.decide_local(rid2, True)
    rid3 = a.propose_write('test','new.py','x=1','NEW')['request_id']
    with a.connect() as c:
        c.execute('UPDATE requests SET payload=? WHERE id=?', ('{}',rid3))
    with pytest.raises(PolicyError):
        a.decide_local(rid3, True)

def test_checkpoint_bytes_unchanged_and_snapshot_filtered(agent):
    a, source = agent
    (source/'checkpoints').mkdir()
    checkpoint = source/'checkpoints'/'model.ckpt'
    checkpoint.write_bytes(b'CHECKPOINT-DO-NOT-TOUCH\0\xff')
    (source/'data').mkdir()
    (source/'data'/'train.txt').write_text('PRIVATE TRAINING')
    before = checkpoint.read_bytes()
    manifest = a.snapshot('test')
    assert set(manifest) == {'hello.py'}
    assert checkpoint.read_bytes() == before
    assert {f['path'] for f in a.listing('test')['items']} == {'hello.py'}

def test_symlink_hardlink_rejected(agent, tmp_path):
    a, source = agent
    outside = tmp_path/'outside.py'
    outside.write_text('private')
    try:
        (source/'link.py').symlink_to(outside)
    except OSError:
        pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(PolicyError):
        a.read('test','link.py')
    os.link(outside, source/'hard.py')
    with pytest.raises(PolicyError):
        a.read('test','hard.py')

def test_no_execution_when_sandbox_absent(agent, monkeypatch):
    a, source = agent
    monkeypatch.setattr('udb.sandbox.available', lambda: False)
    with pytest.raises(PolicyError, match='Host execution is disabled'):
        a.propose_job('test','python', 'open("C:/danger", "w").write("bad")')

def test_syntax_parse_does_not_execute(agent):
    a, source = agent
    (source/'hello.py').write_text('raise RuntimeError("must not execute")\n')
    assert a.syntax_check('test','hello.py')['valid']
    (source/'hello.py').write_text('def broken(:')
    assert not a.syntax_check('test','hello.py')['valid']

def test_wsb_only_maps_job_and_runtime_no_network(tmp_path):
    import xml.etree.ElementTree as ET
    xml = build_wsb(tmp_path/'input', tmp_path/'output', tmp_path/'Python311')
    root = ET.fromstring(xml)
    assert root.findtext('Networking') == 'Disable'
    assert root.findtext('ClipboardRedirection') == 'Disable'
    maps = root.findall('./MappedFolders/MappedFolder')
    assert len(maps) == 3
    assert [m.findtext('ReadOnly') for m in maps] == ['true','false','true']
    assert root.findtext('ProtectedClient') == 'Enable'

def test_internal_source_and_state_cannot_be_projects(agent):
    a, source = agent
    private = a.home/'udb'
    private.mkdir()
    with pytest.raises(PolicyError):
        a.register_local('self',str(private), True)
    with pytest.raises(PolicyError):
        a.register_local('state',str(a.state), True)

def test_hardlink_alone_blocked(agent, tmp_path):
    a, source = agent
    outside = tmp_path/'other.py'
    outside.write_text('secret')
    os.link(outside, source/'hard.py')
    with pytest.raises(PolicyError):
        a.read('test','hard.py')

def test_junction_blocked_on_windows(agent, tmp_path):
    if os.name != 'nt':
        pytest.skip('Windows-specific junction')
    import subprocess
    a, source = agent
    outside = tmp_path/'elsewhere'
    outside.mkdir()
    (outside/'secret.py').write_text('private')
    subprocess.run(['cmd','/c','mklink','/J',str(source/'junction'),str(outside)], check=True, capture_output=True)
    try:
        with pytest.raises(PolicyError):
            a.read('test','junction/secret.py')
    finally:
        (source/'junction').rmdir()
