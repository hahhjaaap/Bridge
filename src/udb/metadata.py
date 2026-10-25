"""Derived plugin metadata: version and tool counts have one source."""
import json
from pathlib import Path
from ._version import VERSION
from .registry import catalog

def manifest():
    data=json.loads(Path(__file__).with_name('plugin.template.json').read_text('utf-8'))
    counts=catalog();data['version']=VERSION
    data['description']=data['description'].replace('{tool_count}',str(counts['tool_count']))
    return data
