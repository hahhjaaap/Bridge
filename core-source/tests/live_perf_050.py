"""Windows acceptance and honest local latency report using the existing demo grant."""
import argparse,ctypes,json,statistics,sys,time,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from udb.core import Agent
from udb import __version__,desktop_vm as vm
def stats(xs):
    s=sorted(xs)
    return {k:round(s[min(len(s)-1,int((len(s)-1)*p))],3) for k,p in [('p50',.5),('p95',.95),('p99',.99)]}
def main():
    sys.stdout.reconfigure(encoding='utf8',errors='replace')
    p=argparse.ArgumentParser();p.add_argument('--home',type=Path,required=True);a=p.parse_args()
    agent=Agent(a.home)
    if 'demo' not in agent.sandbox_autonomy_projects():raise RuntimeError('Existing demo guest grant required')
    report={'version':__version__,'scope':'local host-to-Sandbox; not ChatGPT end-to-end','checks':{},'measurements':{}}
    cpu=time.process_time();wall=time.monotonic()
    def record(name,func):
        t=time.perf_counter()
        try:
            value=func();report['checks'][name]={'passed':True,'ms':round((time.perf_counter()-t)*1000,3)}
            print(name,'PASS',report['checks'][name]['ms'],flush=True);return value
        except Exception as exc:
            report['checks'][name]={'passed':False,'error':str(exc)[:500]}
            print(name,'FAIL',str(exc)[:300],flush=True);return None
    proposal=agent.propose_desktop('demo','UDB dev2 native Notepad Unicode, Explorer, screenshot and 100-action local benchmark in Sandbox only',600)
    if proposal['status']!='queued_sandbox':raise RuntimeError('Existing grant did not authorize session')
    sid=proposal['request_id'];report['session_id']=sid
    try:
        agent.pump_sandbox_queue_local()
        until=time.monotonic()+100
        while time.monotonic()<until:
            st=vm.status(agent,sid)
            if st['ready']:break
            if st['status']!='running':raise RuntimeError(str(st))
            time.sleep(.5)
        else:raise RuntimeError('Guest readiness timeout')
        report['capabilities']=st.get('capabilities')
        def call(action,**kw):return vm.command(agent,sid,action,**kw)
        call('state')
        for action in ('state',):
            samples=[]
            for i in range(100):
                t=time.perf_counter();last=call(action);samples.append((time.perf_counter()-t)*1000)
            report['measurements'][action+'_ms']={**stats(samples),'count':len(samples),'samples':samples,'last_timings':last.get('timings')}
            print(action,stats(samples),flush=True)
        singles=[];batches=[]
        for i in range(10):
            t=time.perf_counter()
            for j in range(6):call('state')
            singles.append((time.perf_counter()-t)*1000)
            t=time.perf_counter();result=call('batch',steps=[{'action':'state'}]*6);batches.append((time.perf_counter()-t)*1000)
            assert result['state']['ok'] and result['state']['completed']==6
        report['measurements']['six_separate_ms']=stats(singles);report['measurements']['six_batch_ms']=stats(batches)
        report['initial_windows']=call('app',operation='list')['state']
        record('notepad_launch',lambda:call('app',operation='open',app='notepad',path='hello.py'))
        title=''
        for i in range(30):
            windows=call('app',operation='list')['state']['windows']
            matches=[r for r in windows if 'notepad' in r['title'].casefold()]
            if matches:title=matches[0]['title'];break
            time.sleep(.2)
        if title:
            report['notepad_window']=title
            record('notepad_focus',lambda:call('app',operation='focus',window_title=title))
            snapshot=record('notepad_snapshot',lambda:call('snapshot',window_title=title))
            if snapshot:
                report['notepad_snapshot']=snapshot['state']
                edits=[r for r in snapshot['state'].get('elements',[]) if r['control_type'] in ('Edit','Document')]
                if len(edits)==1:
                    target={'automation_id':edits[0]['automation_id']} if edits[0].get('automation_id') else {'control_type':edits[0]['control_type']}
                    unicode='UDB tiếng Việt: Xin chào thế giới! Đường dẫn thử nghiệm.'
                    def verify_text():
                        result=call('element_set_text',window_title=title,value=unicode,**target)
                        if not result['state'].get('verified'):raise RuntimeError('No independent text verification')
                        return result
                    record('notepad_unicode_verified',verify_text)
                    record('notepad_ctrl_s',lambda:call('press',keys='ctrl+s'))
                else:report['checks']['notepad_unicode_verified']={'passed':False,'error':'Expected exactly one text control'}
            record('notepad_resize',lambda:call('app',operation='resize',window_title=title,x=20,y=20,width=800,height=500))
        def capture():
            result=call('screenshot',x=20,y=20,width=800,height=500,max_width=640)
            (ROOT/'docs'/'notepad-guest.png').write_bytes(result['_png'])
            report['screenshot']={'bytes':len(result['_png']),'capture':result['state'].get('capture'),'timings':result.get('timings')}
            return result
        record('guest_region_screenshot',capture)
        record('explorer_launch',lambda:call('app',operation='open',app='explorer'))
        time.sleep(1)
        report['final_windows']=call('app',operation='list')['state']
        if title:record('multiwindow_notepad_focus',lambda:call('app',operation='focus',window_title=title))
        # Access-denial checks issue no guest action.
        def denied():
            try:call('batch',steps=[{'action':'state','shell':'cmd'}])
            except ValueError:return True
            raise RuntimeError('Unexpectedly allowed unknown step field')
        record('batch_unauthorized_field_denied',denied)
        record('stop',lambda:vm.stop(agent,sid))
    except Exception as exc:
        report['fatal_error']=str(exc);print(traceback.format_exc(),flush=True)
    finally:
        vm.close_owned(agent)
        report['host_cpu_seconds']=time.process_time()-cpu
        report['elapsed_seconds']=time.monotonic()-wall
        report['host_cpu_percent_one_core']=100*report['host_cpu_seconds']/report['elapsed_seconds']
        (ROOT/'docs'/'benchmark-050.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
        print('REPORT_SAVED',flush=True)
if __name__=='__main__':main()