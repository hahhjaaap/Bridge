"""Windows developer build without changing PowerShell execution policy."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from udb.release import write_build_info

def run(*args, stdout=None):
    subprocess.run([str(x) for x in args],check=True,stdout=stdout)

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--revision',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parent
    os.chdir(root)
    output=args.output.resolve()
    output.mkdir(parents=True,exist_ok=False)
    # Fail before freezing if the GUI source or release policy fails static checks.
    run(sys.executable,'-m','ruff','format','--check','udb/manager.py')
    run(sys.executable,'-m','ruff','check','udb/manager.py','udb/release_policy.py',
        'tests/test_manager_lifecycle.py')
    run(sys.executable,'-m','mypy','--config-file','pyproject.toml',
        'udb/manager.py','udb/release_policy.py')
    run(sys.executable,'-m','pytest','tests','-q')
    write_build_info(root,args.revision)
    run(sys.executable,'-m','PyInstaller','--noconfirm','--clean',
        '--distpath',output/'bin','--workpath',output/'work','UniversalDesktopBridge.spec')
    exe=output/'bin'/'UniversalDesktopBridge.exe'
    run(exe,'--version')
    with (output/'tool-catalog.json').open('w',encoding='utf-8') as destination:
        run(exe,'--list-tools',stdout=destination)
    if json.loads((output/'tool-catalog.json').read_text('utf-8'))['tool_count'] != 35:
        raise RuntimeError('Unexpected tool catalog; review API before release')
    run(exe,'--self-test-credentials')
    run(exe,'--gui-check','--gui-qa-dir',output/'gui-qa')
    run(sys.executable,'tests/smoke_mcp.py','--exe',exe,'--isolated')
    with (output/'headless-live-acceptance.json').open('w',encoding='utf-8') as destination:
        run(sys.executable,'tests/live_headless.py','--exe',exe,stdout=destination)
    with (output/'computer-live-acceptance.json').open('w',encoding='utf-8') as destination:
        run(sys.executable,'tests/live_computer.py','--exe',exe,stdout=destination)
    with (output/'host-live-acceptance.json').open('w',encoding='utf-8') as destination:
        run(sys.executable,'tests/live_host_background.py','--exe',exe,stdout=destination)
    from udb import __version__
    run(sys.executable,'Make-Package.py','--exe',exe,'--output',
        output/f'Universal-Desktop-Bridge-{__version__}-Windows.zip')

if __name__=='__main__':
    main()
