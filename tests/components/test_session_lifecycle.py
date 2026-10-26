"""Verify Companion session cleanup and bounded retained session history."""

from __future__ import annotations

import sys
import tempfile

from udb_companion import MAX_RETAINED_SESSIONS, Runtime


def test_process_shutdown():
    with tempfile.TemporaryDirectory(prefix="udb-lifecycle-") as root:
        rt = Runtime(root, enable_terminal=True)
        task = rt.start(
            sys.executable, ["-u", "-c", "import time; time.sleep(60)"], ".", 0
        )
        sid = task["session_id"]
        owned = rt.sessions[sid].process
        assert owned.poll() is None, "A long-running test process ended unexpectedly"
        rt.shutdown()
        assert owned.poll() is not None, "Owned child still running after shutdown"
        print("PASS | MCP shutdown stops owned long-running process")


def test_bounded_session_history():
    with tempfile.TemporaryDirectory(prefix="udb-history-") as root:
        rt = Runtime(root, enable_terminal=True)
        try:
            for _ in range(MAX_RETAINED_SESSIONS + 8):
                task = rt.start(sys.executable, ["-u", "-c", "pass"], ".", 0)
                rt.sessions[task["session_id"]].process.wait(timeout=4)
            assert len(rt.sessions) <= MAX_RETAINED_SESSIONS, len(rt.sessions)
            print("PASS | Finished session history bounded to", MAX_RETAINED_SESSIONS)
        finally:
            rt.shutdown()


if __name__ == "__main__":
    test_process_shutdown()
    test_bounded_session_history()
