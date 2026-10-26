"""Kill real MCP subprocesses; verify recovery never replays a side effect."""

import concurrent.futures
import json
import sys
import tempfile
import unittest
from pathlib import Path

from udb_recovery import RecoveringChild
from udb_unified import StdioChild

SCRIPT = r"""
import json,os,sys
from pathlib import Path
root=Path(sys.argv[1])
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 method=m['method'];a=m.get('params',{})
 if method=='initialize':r={'protocolVersion':'2024-11-05','capabilities':{}}
 elif method=='tools/list':r={'tools':[{'name':'changed' if (root/'drift').exists() else 'sample','inputSchema':{'type':'object'}}]}
 elif method=='tools/call' and a.get('name')=='write_and_die':
  with (root/'writes').open('a') as f:f.write('one\n')
  os._exit(12)
 else:r={'writes':(root/'writes').read_text().count('one') if (root/'writes').exists() else 0}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':r}),flush=True)
"""


class Recovery(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="udb-recovery-")
        self.root = Path(self.temp.name)
        self.backend = RecoveringChild(
            lambda: StdioChild(
                "fixture",
                [sys.executable, "-u", "-c", SCRIPT, str(self.root)],
                str(self.root),
                lambda m: None,
            ),
            "fixture",
        )
        self.backend.start()
        self.backend.request(
            "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}
        )
        self.backend.request("tools/list")

    def tearDown(self):
        self.backend.close()
        self.temp.cleanup()

    def kill(self):
        self.backend.process.kill()
        self.backend.process.wait(timeout=5)

    def test_next_call_recovers_a_dead_process(self):
        self.kill()
        self.assertEqual(
            self.backend.request("tools/call", {"name": "status"})["result"]["writes"],
            0,
        )
        self.assertEqual(self.backend.status()["generation"], 2)

    def test_unknown_write_outcome_is_not_replayed(self):
        with self.assertRaises(RuntimeError):
            self.backend.request("tools/call", {"name": "write_and_die"})
        self.assertEqual(
            self.backend.request("tools/call", {"name": "status"})["result"]["writes"],
            1,
        )
        self.assertEqual((self.root / "writes").read_text(), "one\n")

    def test_concurrent_callers_share_one_restart(self):
        self.kill()
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            rows = list(
                pool.map(
                    lambda _: self.backend.request("tools/call", {"name": "status"}),
                    range(12),
                )
            )
        self.assertEqual(len(rows), 12)
        self.assertEqual(self.backend.status()["generation"], 2)

    def test_schema_drift_is_rejected_before_new_calls(self):
        (self.root / "drift").touch()
        self.kill()
        with self.assertRaisesRegex(RuntimeError, "schema changed"):
            self.backend.request("tools/call", {"name": "write_and_die"})
        self.assertFalse((self.root / "writes").exists())

    def test_cleanup_and_close_never_resurrect(self):
        self.kill()
        self.backend.request("udb/close-sessions")
        self.assertEqual(self.backend.status()["generation"], 1)
        self.backend.close()
        with self.assertRaises(RuntimeError):
            self.backend.request("tools/call", {"name": "status"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
