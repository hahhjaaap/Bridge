"""Persistent guest UIA protocol regression tests, no guest or host input."""
import base64
import json
import queue
import threading
import pytest
from udb import desktop_guest

class FakePipe:
    def __init__(self):
        self.q=queue.Queue()
        self.requests=[]
        self.q.put('READY\n')
        self.closed=False
    def __iter__(self):
        while not self.closed:
            try: yield self.q.get(timeout=1)
            except queue.Empty: return
    def readline(self,size=-1):
        if self.closed:return ''
        try:return self.q.get(timeout=1)
        except queue.Empty:return ''
    def write(self,line):
        request=json.loads(base64.b64decode(line.strip()).decode('utf-8'))
        self.requests.append(request)
        if request['name']=='DENY':
            self.q.put('ERR:Element not found\n')
        elif request['mode']=='snapshot':
            self.q.put('RES:{"ok":true,"active_window":"Demo","elements":[]}\n')
        else:
            self.q.put('RES:{"ok":true,"found":true}\n')
    def flush(self): pass
    def close(self): self.closed=True

def test_guest_uia_reuses_process_and_binds_window(monkeypatch):
    launch=[]
    def start(argv,**kwargs):
        proc=type('FakeProcess',(),{})()
        proc.stdin=FakePipe()
        proc.stdout=proc.stdin
        proc.stderr=None
        proc.poll=lambda:None
        proc.wait=lambda timeout=None:0
        launch.append((argv,proc))
        return proc
    monkeypatch.setattr(desktop_guest.subprocess,'Popen',start)
    guest=desktop_guest.GuestDesktop.__new__(desktop_guest.GuestDesktop)
    assert guest.accessibility('snapshot')['active_window']=='Demo'
    assert guest.accessibility('wait_for',name='Confirm')['found'] is True
    assert len(launch)==1
    args=launch[0][1].stdin.requests
    assert args[1]['window_title']=='Demo'
    assert args[0]['mode']=='snapshot'
    assert args[1]['mode']=='wait_for'
    guest._close_uia()

def test_guest_uia_does_not_fallback_on_denied_action(monkeypatch):
    def start(argv,**kwargs):
        proc=type('FakeProcess',(),{})()
        proc.stdin=FakePipe()
        proc.stdout=proc.stdin
        proc.stderr=None
        proc.poll=lambda:None
        proc.wait=lambda timeout=None:0
        return proc
    monkeypatch.setattr(desktop_guest.subprocess,'Popen',start)
    guest=desktop_guest.GuestDesktop.__new__(desktop_guest.GuestDesktop)
    with pytest.raises(RuntimeError,match='Element not found'):
        guest.accessibility('wait_for',name='DENY')
    guest._close_uia()

def test_script_reuses_uia_element_without_host_fallback():
    script=(__import__('pathlib').Path(desktop_guest.__file__).parent/'desktop_accessibility.ps1').read_text('utf-8')
    assert 'cachedTop' in script and 'ReadLine()' in script
    assert 'WDAGUtilityAccount' in script
    assert 'SetCursorPos' not in script
    assert 'SendInput' not in script
