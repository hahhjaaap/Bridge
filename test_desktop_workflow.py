"""Real MCP advanced GUI acceptance, exclusively on an owned native test form."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
from test_uia_provider import Client,object_data,receive,ROOT


def main():
    if os.name!='nt':raise RuntimeError('Windows required')
    powershell=str(Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
    script=(ROOT/'uia_provider_fixture.ps1').read_text(encoding='utf-8-sig')
    script=script.replace('p.ExStyle |= 0x08000000;','')  # own form: permit ordinary foreground input
    form=subprocess.Popen([powershell,'-NoProfile','-NonInteractive','-Sta','-Command',script],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    rows=queue.Queue();reader=threading.Thread(target=receive,args=(form.stdout,rows),daemon=True);reader.start()
    client=None;events=[]
    def event(kind,timeout=5):
        end=time.monotonic()+timeout
        while time.monotonic()<end:
            row=rows.get(timeout=max(.01,end-time.monotonic()));events.append(row)
            if row.get('event')==kind:return row
        raise AssertionError('Missing fixture event '+kind)
    try:
        ready=event('ready',15);client=Client()
        def call(**args):return object_data(client.tool(**args))
        windows=call(action='list')['windows'];target=[x for x in windows if x['pid']==form.pid and x['title']==ready['title']]
        assert len(target)==1;wid=target[0]['window_id']
        displays=call(action='displays')['displays'];assert displays and all('work_area' in m for m in displays)
        work=next((m['work_area'] for m in displays if m['primary']),displays[0]['work_area'])
        call(action='window_state',window_id=wid,window_state='restore')
        call(action='resize_window',window_id=wid,width=700,height=480)
        moved=call(action='move_window',window_id=wid,x=work[0]+40,y=work[1]+40)
        assert moved['bounds'][:2]==[work[0]+40,work[1]+40]
        assert call(action='wait_for',window_id=wid,automation_id='Calculate',control_type='Button',condition='enabled',wait_ms=2000)['matched']
        assert call(action='wait_for',window_id=wid,automation_id='does-not-exist',condition='exists',wait_ms=150)['timed_out']
        assert call(action='wait_for',window_id=wid,automation_id='does-not-exist',condition='absent',wait_ms=150)['matched']
        batch=call(action='batch',window_id=wid,steps=[
            dict(action='set_value',automation_id='OrderName',control_type='Edit',text='GUI workflow 🚀'),
            dict(action='set_value',automation_id='Quantity',control_type='Edit',text='4'),
            dict(action='set_value',automation_id='UnitPrice',control_type='Edit',text='125000'),
            dict(action='invoke',automation_id='Calculate',control_type='Button')])
        assert batch['ok'] and batch['completed_steps']==4
        assert event('calculate')['output']=='GUI workflow 🚀 | Tổng tiền: 500000 VND'
        failed=client.tool(action='batch',window_id=wid,steps=[dict(action='set_value',automation_id='Quantity',text='5'),dict(action='invoke',automation_id='missing'),dict(action='invoke',automation_id='Calculate')])
        assert failed.get('isError')
        partial=None
        for block in failed['content']:
            try:partial=json.loads(block.get('text',''))
            except ValueError:continue
        assert partial['completed_steps']==1 and partial['failed_index']==1
        # Physical input is limited to the visible, test-owned panel. Stop the
        # test on takeover/refusal; never retry an unknown injected action.
        snap=call(action='snapshot',window_id=wid)
        panel=next(e for e in snap['elements'] if e.get('automation_id')=='DragPad')
        x,y,width,height=panel['bounds'];x=int(x);y=int(y)
        call(action='focus',window_id=wid)
        call(action='mouse_move',window_id=wid,x=x+30,y=y+30,duration_ms=0)
        call(action='click',window_id=wid,x=x+30,y=y+30,button='right')
        assert event('mouse_down')['button']=='Right';assert event('mouse_up')['button']=='Right'
        call(action='click',window_id=wid,x=x+50,y=y+30,button='left',clicks=2)
        event('native_double_click')
        call(action='drag',window_id=wid,from_x=x+30,from_y=y+60,x=x+180,y=y+70,duration_ms=150)
        end=time.monotonic()+5
        while True:
            up=event('mouse_up',max(.01,end-time.monotonic()))
            if abs(up.get('x',0)-180)<=2:break
        call(action='scroll',window_id=wid,x=x+40,y=y+40,delta=120,horizontal=True)
        event('horizontal_wheel')
        shot=client.tool(action='screenshot',window_id=wid,region=[x,y,x+100,y+80])
        assert not shot.get('isError') and any(b.get('type')=='image' for b in shot['content'])
        call(action='invoke',window_id=wid,automation_id='CloseTest',control_type='Button');event('closed')
        assert form.wait(timeout=5)==0
        print('PASS | Real MCP GUI: display/DPI, restore/move/resize, waits, batch/partial failure, right/double click, pointer move, drag, horizontal wheel and region screenshot; only owned form targeted')
    finally:
        if form.poll() is None:form.terminate();form.wait(timeout=5)
        reader.join(timeout=2);form.stdout.close()
        if client:client.close()


if __name__=='__main__':main()
