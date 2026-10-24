"""Disposable Windows acceptance worker. Never opens production state."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import time
from .core import Agent, PolicyError
from .host_client import atomic_json
from .host_service import HostService


FIXTURE = '''<!doctype html><meta charset="utf-8"><title>UDB owned fixture</title>
<h1>Private UDB acceptance fixture</h1><input id="name"><button id="confirm"
onclick="document.querySelector('#result').innerText=document.querySelector('#name').value">Confirm</button><p id="result"></p>'''


def run(home):
    home = Path(home).resolve()
    temp = Path(tempfile.gettempdir()).resolve()
    if home.parent != temp or not home.name.startswith('udb-hostqa-') or not (home/'QA-ISOLATED').is_file():
        raise PolicyError('Host acceptance requires a marked disposable OS temp home')
    agent = Agent(home)
    if agent.projects() or (agent.state/'connection.json').exists():
        raise PolicyError('Host acceptance refuses configured owner state')
    atomic_json(agent.state/'host-settings.json', dict(enabled=True))
    service = HostService(agent)
    try:
        deadline = time.monotonic()+60
        while not service.resource.get('admission_ready'):
            if time.monotonic() > deadline: raise PolicyError('Machine is too busy for host acceptance')
            time.sleep(.2)
        with service.lock:
            result = service.dispatch('start', dict(kind='browser', task='Disposable acceptance fixture', duration_seconds=180))
            obj = service.sessions[result['session_id']]['object']; tab = result['tabs'][0]['tab_id']
            obj.deadline = time.monotonic()+10
            frame = obj.call('Page.getFrameTree', session=obj.sid(tab))['frameTree']['frame']['id']
            obj.call('Page.setDocumentContent', dict(frameId=frame, html=FIXTURE), obj.sid(tab))
            service.publish()
            atomic_json(home/'fixture-ready.json', dict(session_id=result['session_id'], tab_id=tab))
        deadline = time.monotonic()+90
        while not (home/'stop.flag').exists() and time.monotonic() < deadline: time.sleep(.1)
    finally: service.close()
