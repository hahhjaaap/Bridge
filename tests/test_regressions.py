"""Run legacy component programs through aliases into the unified package."""
import os
from pathlib import Path
import subprocess
import sys
import pytest
from udb.launcher import child_environment
ROOT=Path(__file__).resolve().parents[1]

@pytest.mark.parametrize('script',[
 'test_audit_regressions.py','test_backend_recovery.py','test_tool_consolidation.py',
 pytest.param('test_extensions.py',marks=pytest.mark.skipif(os.name!='nt',reason='Legacy atomic move acceptance requires Windows')),'test_features.py','test_terminal_utf8.py','test_session_lifecycle.py',
 'test_integration.py','test_documents.py','test_work_recovery.py','test_conpty.py','test_windows_durability.py',
])
def test_component_program(script):
    result=subprocess.run([sys.executable,str(ROOT/script)],cwd=ROOT,env=child_environment(),capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stdout+'\n'+result.stderr
