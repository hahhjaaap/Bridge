"""Real File Explorer acceptance through UDB guest engine, no demo app."""
import ctypes,json,sys,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from udb.core import Agent
from udb import desktop_vm as vm
def main():
    sys.stdout.reconfigure(encoding='utf8',errors='replace')
    a=Agent(ROOT.parents[1]);report={}
    p=a.propose_desktop('demo','UDB real Windows File Explorer UIA, Unicode, focus, resize and screenshot acceptance inside Sandbox',260)
    if p['status']!='queued_sandbox':raise RuntimeError('Existing Sandbox grant required')
    sid=p['request_id']
    def call(action,**kw):return vm.command(a,sid,action,**kw)
    def check(name,fn):
        t=time.perf_counter()
        try:
            result=fn();report[name]={'passed':True,'ms':round((time.perf_counter()-t)*1000,3),'result':result}
            print(name,'PASS',flush=True);return result
        except Exception as exc:report[name]={'passed':False,'error':str(exc)};print(name,'FAIL',str(exc),flush=True)
    try:
        a.pump_sandbox_queue_local()
        until=time.monotonic()+95
        while time.monotonic()<until:
            if vm.status(a,sid)['ready']:break
            time.sleep(.5)
        else:raise RuntimeError('Guest not ready')
        call('app',operation='open',app='explorer')
        title=''
        for _ in range(50):
            windows=call('app',operation='list')['state']['windows']
            titles=[r['title'] for r in windows if 'file explorer' in r['title'].casefold()]
            if titles:
                title=titles[0]
                window_id=next(r['window_id'] for r in windows if r['title']==title)
                break
            time.sleep(.1)
        if not title:raise RuntimeError('Explorer window not found')
        report['window']=title
        check('focus',lambda:call('app',operation='focus',window_id=window_id))
        snap=check('snapshot',lambda:call('snapshot',window_id=window_id))
        if snap:
            edits=[r for r in snap['state']['elements'] if r['control_type']=='Edit']
            report['edit_candidates']=edits
            targets=[r for r in edits if 'search' in (r.get('name','')+r.get('automation_id','')).casefold()]
            if len(targets)==1:
                target=targets[0];text='Kiểm thử tiếng Việt Đường phố'
                def unicode():
                    r=call('element_set_text',window_id=window_id,automation_id=target['automation_id'],name=target['name'],value=text)
                    if not r['state'].get('verified'):raise RuntimeError('Text operation not independently verified')
                    return r
                check('unicode_verified',unicode)
        def resize():
            r=call('app',operation='resize',window_id=window_id,x=30,y=30,width=1000,height=650)
            if not r['state']['verified']:raise RuntimeError('Window bounds did not match')
            return r
        check('resize_verified',resize)
        samples=[]
        for _ in range(10):
            t=time.perf_counter();call('snapshot',window_id=window_id);samples.append((time.perf_counter()-t)*1000)
        report['warm_snapshot_samples_ms']=samples
        def screenshot():
            r=call('screenshot',x=30,y=30,width=1000,height=650,max_width=1000)
            png=r.pop('_png');(ROOT/'docs'/'explorer-guest.png').write_bytes(png);r['bytes']=len(png);return r
        check('screenshot',screenshot)
    except Exception as exc:report['fatal_error']=str(exc);print(traceback.format_exc(),flush=True)
    finally:
        vm.close_owned(a)
        (ROOT/'docs'/'explorer-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
if __name__=='__main__':main()
