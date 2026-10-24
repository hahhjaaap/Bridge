import json
import os
import subprocess
import sys
from pathlib import Path
from udb.core import Agent

base = Path(__file__).resolve().parent
agent = Agent(base)
demo = base/'projects'/'demo'
demo.mkdir(parents=True, exist_ok=True)
hello = demo/'hello.py'
if not hello.exists():
    hello.write_text('def add(a, b):\n    return a + b\n\nif __name__ == "__main__":\n    print("Universal Desktop Bridge demo:", add(2, 3))\n', encoding='utf-8')
if not any(p['id']=='demo' for p in agent.projects()):
    agent.register_local('demo', str(demo), writable=True)
runtime = agent.state/'runtime.json'
if not runtime.exists():
    runtime.write_text(json.dumps({'python_runtime': sys.base_prefix}), encoding='utf-8')
if os.name == 'nt':
    # Protect state from other ordinary users; do not change any project/data ACLs.
    who = subprocess.check_output(['whoami'], text=True).strip()
    subprocess.run(['icacls', str(agent.state), '/inheritance:r', '/grant:r',
                    f'{who}:(OI)(CI)F', 'SYSTEM:(OI)(CI)F'], check=True, capture_output=True)
print(json.dumps(agent.diagnostics(), indent=2))
