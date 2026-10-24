"""Launch the real entrypoint; verify loopback HTTP auth and MCP protocol."""
import asyncio
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

home = Path(__file__).resolve().parents[1]
exe = home/'UniversalDesktopBridge.exe'
argv = [str(exe)] if exe.exists() else [sys.executable, str(home/'bridge.py')]
with (home/'state'/'http-test.log').open('wb') as log:
    proc = subprocess.Popen(argv+['--serve','--home',str(home)], stdout=log, stderr=log)
    try:
        for attempt in range(60):
            try:
                urllib.request.urlopen('http://127.0.0.1:17843/mcp', timeout=1)
            except urllib.error.HTTPError as e:
                assert e.code == 401
                break
            except OSError:
                time.sleep(.25)
        else:
            raise RuntimeError('HTTP server did not become ready; inspect state/http-test.log')
        print(json.dumps({'unauthenticated_http':401}))
        subprocess.run([sys.executable, str(home/'tests'/'smoke_mcp.py'), '--http'], check=True)
    finally:
        if os.name == 'nt' and proc.poll() is None:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'], capture_output=True)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
