"""Single process command builder for installed, editable and frozen builds."""
import os
from pathlib import Path
import sys

def command(arguments=()):
    prefix=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,'-m','udb']
    return [*prefix,*map(str,arguments)]

def worker_command(role,arguments=()):return command(['_worker',role,*arguments])

def child_environment():
    env={k:v for k,v in os.environ.items() if k not in ('CONTROL_PLANE_API_KEY','TUNNEL_CLIENT_PROFILE_DIR','_MEIPASS2') and not k.startswith('_PYI_')}
    if getattr(sys,'frozen',False):env['PYINSTALLER_RESET_ENVIRONMENT']='1'
    else:
        # Editable checkouts must also resolve the package from arbitrary cwd.
        package_root=str(Path(__file__).resolve().parent.parent)
        env['PYTHONPATH']=os.pathsep.join([package_root,*([env['PYTHONPATH']] if env.get('PYTHONPATH') else [])])
    env['PYTHONIOENCODING']='utf-8';env['PYTHONUTF8']='1'
    return env
