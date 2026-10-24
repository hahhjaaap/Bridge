"""Task safety, live protocol payloads and source policy regressions."""
import io,json,time
from types import SimpleNamespace
from pathlib import Path
import pytest
from udb import desktop_guest as guest,desktop_vm as vm
from udb.core import Agent,PolicyError,canonical
from udb.fileops import search_files,read_files,propose_text_edit
from test_command_mailbox_049 import desktop

@pytest.mark.parametrize('steps',[
    [],[{'action':'state'}]*13,[{'action':'batch','steps':[]}],
    [{'action':'state','shell':'cmd'}],[{'action':'app','operation':'close'}],
    [{'action':'type','text':'a'*5001}],None])
def test_batch_rejects_unbounded_or_extra_capabilities(steps):
    with pytest.raises((PolicyError,ValueError)):vm.validate_action('batch',steps=steps)

def test_batch_validates_each_step_before_dispatch():
    with pytest.raises(PolicyError):vm.validate_action('batch',steps=[{'action':'click','x':99999}])
    assert vm.validate_action('batch',steps=[{'action':'state'},{'action':'app','operation':'list'}])['steps']

def test_batch_stops_at_first_failure_and_reports_completed(tmp_path,monkeypatch):
    monkeypatch.setattr(guest,'INPUT',tmp_path);monkeypatch.setattr(guest,'OUTPUT',tmp_path)
    obj=guest.GuestDesktop.__new__(guest.GuestDesktop);obj.session_id='a'*32
    calls=[]
    def step(c):
        calls.append(c)
        if len(calls)==2:raise RuntimeError('denied')
        return {'verified':True}
    obj.step=step
    result=obj.batch({'steps':[{'action':'state'}]*3,'expires':time.time()+10,'request_id':'b'*32})
    assert result['ok'] is False and result['failed_index']==1 and result['completed']==1
    assert len(calls)==2
    progress=json.loads((tmp_path/'progress.json').read_text())
    assert progress['completed']==1

def test_batch_honors_stop_between_steps(tmp_path,monkeypatch):
    monkeypatch.setattr(guest,'INPUT',tmp_path);monkeypatch.setattr(guest,'OUTPUT',tmp_path)
    obj=guest.GuestDesktop.__new__(guest.GuestDesktop);obj.session_id='a'*32
    obj.step=lambda c:pytest.fail('must stop before action')
    (tmp_path/'cancel-mailbox.json').write_text(json.dumps({'session_id':obj.session_id}))
    with pytest.raises(RuntimeError,match='cancelled'):
        obj.batch({'steps':[{'action':'state'}],'expires':time.time()+10,'request_id':'b'*32})

def test_monitor_tolerates_reply_renamed_during_scan(tmp_path,monkeypatch):
    (tmp_path/'replies').mkdir();(tmp_path/'replies'/'reply.tmp').write_text('transient')
    original=vm.reject_links
    def vanish(path):
        if path.name=='reply.tmp':
            path.unlink(missing_ok=True);raise FileNotFoundError(str(path))
        original(path)
    monkeypatch.setattr(vm,'reject_links',vanish)
    assert vm.output_bytes(tmp_path)==0

def test_reply_identity_mismatch_never_acknowledges_action(desktop,monkeypatch):
    agent,sid,source,output=desktop
    def reply(_):
        c=guest.read_command_mailbox(source/'command-mailbox.json')
        (output/'replies'/(c['request_id']+'.json')).write_text(canonical({'ok':True,'request_id':'d'*32,'session_id':sid}))
    monkeypatch.setattr(vm.time,'sleep',reply)
    with pytest.raises(PolicyError,match='identity mismatch'):vm.command(agent,sid,'state')
    with agent.connect() as c:assert c.execute('SELECT count(*) FROM desktop_control').fetchone()[0]==0

def test_large_uia_reply_survives_reader_without_truncation(monkeypatch):
    payload={'ok':True,'active_window':'Many controls','elements':[{'name':'x'*100,'automation_id':str(i)} for i in range(90)]}
    output='READY\nRES:'+json.dumps(payload)+'\n'
    class Proc:
        stdin=io.StringIO();stdout=io.StringIO(output);stderr=None
        def poll(self):return None
        def wait(self,timeout=None):return 0
    monkeypatch.setattr(guest.subprocess,'Popen',lambda *a,**kw:Proc())
    obj=guest.GuestDesktop.__new__(guest.GuestDesktop)
    result=obj.accessibility('snapshot')
    assert len(result['elements'])==90 and result['elements'][-1]['automation_id']=='89'
    obj._close_uia()

def test_source_search_hides_credentials_and_reads_many_files(tmp_path):
    root=tmp_path/'project';root.mkdir();(root/'nested').mkdir()
    (root/'a.py').write_text('hello = 1\n')
    (root/'nested'/'b.py').write_text('hello = 2\n')
    (root/'.env').write_text('hello=SECRET')
    (root/'dataset').mkdir();(root/'dataset'/'leak.py').write_text('hello=SECRET')
    a=Agent(tmp_path/'home');a.register_local('demo',str(root),True)
    result=search_files(a,'demo',pattern='*.py',text='hello')
    assert {r['path'] for r in result['items']}=={'a.py','nested/b.py'}
    assert len(read_files(a,'demo',['a.py','nested/b.py'])['files'])==2
    with pytest.raises(PolicyError):read_files(a,'demo',['../escape.py'])
    sha=a.read('demo','a.py')['sha256']
    p=propose_text_edit(a,'demo','a.py','1','3',sha)
    assert p['status']=='pending_local_approval' and (root/'a.py').read_text()=='hello = 1\n'
    with pytest.raises(PolicyError,match='count'):propose_text_edit(a,'demo','a.py','absent','3',sha)
    with pytest.raises(PolicyError,match='changed'):propose_text_edit(a,'demo','a.py','1','3','f'*64)

def test_screenshot_rejects_invalid_scaling():
    for kwargs in ({'max_width':12},{'width':-1},{'height':99999},{'max_width':True}):
        with pytest.raises(PolicyError):vm.validate_action('screenshot',**kwargs)
    assert vm.validate_action('screenshot',width=800,height=600,max_width=400)['max_width']==400

def test_window_id_validation_does_not_accept_raw_handles():
    for value in ('12345','../escape','A'*32,42):
        with pytest.raises(PolicyError):vm.validate_action('snapshot',window_id=value)
    assert vm.validate_action('snapshot',window_id='a'*32)['window_id']=='a'*32

def test_observed_window_id_survives_title_change_and_rejects_stale():
    import ctypes
    from ctypes import wintypes
    from types import SimpleNamespace
    obj=guest.GuestDesktop.__new__(guest.GuestDesktop)
    token='a'*32
    obj._window_refs={token:(123,77,'CabinetWClass')}
    rows=[{'window_id':token,'title':'Work - File Explorer'}]
    obj.windows=lambda:{'windows':rows}
    def process(hwnd,pid):
        ctypes.cast(pid,ctypes.POINTER(wintypes.DWORD)).contents.value=77
        return 1
    obj.u=SimpleNamespace(GetWindowThreadProcessId=process)
    assert obj.resolve_window(token)==(123,'Work - File Explorer')
    rows[0]['title']='Search results - File Explorer'
    assert obj.resolve_window(token)[1]=='Search results - File Explorer'
    rows.clear()
    with pytest.raises(ValueError,match='stale'):obj.resolve_window(token)

def test_batch_sized_reply_uses_bounded_two_megabyte_reader(desktop,monkeypatch):
    agent,sid,source,output=desktop
    blob='x'*100000
    def reply(_):
        c=guest.read_command_mailbox(source/'command-mailbox.json')
        (output/'replies'/(c['request_id']+'.json')).write_text(canonical({
            'ok':True,'request_id':c['request_id'],'session_id':sid,'state':{'text':blob}}))
    monkeypatch.setattr(vm.time,'sleep',reply)
    assert len(vm.command(agent,sid,'state')['state']['text'])==len(blob)
