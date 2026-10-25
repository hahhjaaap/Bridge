"""Common MCP status metadata without changing legacy payloads or names."""
import json
from ._version import VERSION

def metadata(status):return dict(version=VERSION,status=status,replay_performed=False,unknown_outcome_must_not_replay=True)

def annotate(result):
    if not isinstance(result,dict):return result
    text=next((b.get('text','') for b in result.get('content',[]) if b.get('type')=='text'),'')
    status='ERROR' if result.get('isError') else 'RUNNING' if text.startswith('RUNNING |') else 'PASS'
    extra=dict(result.get('_meta') or {})
    extra['udb']=metadata(status)
    return dict(result,_meta=extra)

def result(data,state=None):
    if state is None:state='RUNNING' if data.get('state')=='running' else 'ERROR' if data.get('state')=='error' else 'PASS'
    return annotate(dict(content=[dict(type='text',text=state+' | UDB Companion'),dict(type='text',text=json.dumps(data,ensure_ascii=False))],structuredContent=data,isError=state=='ERROR'))
