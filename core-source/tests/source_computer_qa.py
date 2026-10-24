"""Fast development fixture; only the fixed native self-test, no permission record."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
with tempfile.TemporaryDirectory(prefix='udb-computerqa-') as temp:
    home=Path(temp)/'agent';home.mkdir();(home/'QA-ISOLATED').write_text('fixed fixture')
    result=subprocess.run([sys.executable,str(Path(__file__).resolve().parents[1]/'bridge.py'),
        '--computer-primitives-qa','--home',str(home)],capture_output=True,text=True,
        encoding='utf-8',errors='replace',timeout=50)
    print(result.stdout);print(result.stderr,file=sys.stderr)
    raise SystemExit(result.returncode)
