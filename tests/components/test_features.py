"""Feature regressions use only disposable projects and test-owned processes."""

import base64
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from udb_background import Background
from udb_companion import Runtime, ScopedFiles
from udb_system_processes import ProcessCatalog


def wait_for(fn, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        value = fn()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("Timed out waiting for owned fixture")


def wait_job(bg, row):
    jid = row["job_id"]
    wait_for(lambda: bg.tasks[jid].state != "running")
    return bg.task(jid)


class Features(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="udb-features-")
        self.root = Path(self.temp.name)
        self.rt = Runtime(str(self.root), True)
        self.bg = self.rt.features.background

    def tearDown(self):
        self.rt.close()
        self.temp.cleanup()

    def test_malformed_rpc_returns_error_and_server_stays_usable(self):
        from tests.components.test_windows_durability import Client

        client = Client(self.root)
        try:
            client.p.stdin.write(b"{bad-json}\n")
            client.p.stdin.flush()
            self.assertEqual(client.q.get(timeout=3)["error"]["code"], -32700)
            with self.assertRaises(AssertionError):
                client.rpc(
                    "tools/call", dict(name="file_read", arguments=["wrong-type"])
                )
            self.assertEqual(client.rpc("ping", {}), {})
        finally:
            client.close()

    def test_python_prompt_interact_and_completion(self):
        start = self.rt.start(sys.executable, ["-u", "-i", "-q"], wait_ms=0)
        sid = start["session_id"]
        session = self.rt.sessions[sid]
        wait_for(lambda: session.prompt()[0])
        row = self.rt.features.tool(
            "terminal_interact",
            dict(session_id=sid, text="print('prompt-fixture')\n", wait_ms=1500),
        )["structuredContent"]
        self.assertTrue(row["prompt_detected"])
        self.assertIn("prompt-fixture", row["output"])
        self.assertTrue(row["waiting_for_input"])
        session.send("exit()\n")
        wait_for(lambda: session.process.poll() is not None)

    def test_input_backpressure_never_blocks_control_requests(self):
        row = self.rt.start(
            sys.executable, ["-c", "import time;time.sleep(20)"], wait_ms=0
        )
        session = self.rt.sessions[row["session_id"]]
        start = time.monotonic()
        entered = threading.Event()
        release = threading.Event()
        original = session.process.stdin

        class GatedInput:
            def write(self, data):
                entered.set()
                if not release.wait(5):
                    raise BrokenPipeError("fixture gate expired")
                return original.write(data)

            def close(self):
                release.set()
                original.close()

        # Synchronize a blocked writer; OS pipe capacity varies by runner.
        try:
            with patch.object(session.process, "stdin", GatedInput()):
                session.send("x" * 10000)
                self.assertTrue(entered.wait(2))
                failures = 0
                start = time.monotonic()
                for _ in range(8):
                    try:
                        session.send("x" * 10000)
                    except RuntimeError:
                        failures += 1
                self.assertGreater(failures, 0)
                self.assertLess(time.monotonic() - start, 1)
                session.stop()
                self.assertIsNotNone(session.process.poll())
        finally:
            release.set()

    def test_terminal_deadline_survives_file_cleanup_error(self):
        with patch.object(
            self.bg, "expire", side_effect=OSError("synthetic cleanup failure")
        ):
            row = self.rt.start(
                sys.executable,
                ["-c", "import time;time.sleep(20)"],
                wait_ms=0,
                timeout_seconds=1,
            )
            session = self.rt.sessions[row["session_id"]]
            wait_for(lambda: session.reason == "timed_out", 4)
            self.assertGreater(self.rt.monitor_errors, 0)
            self.assertIsNotNone(session.process.poll())

    def test_terminal_deadline(self):
        row = self.rt.start(
            sys.executable,
            ["-c", "import time;time.sleep(20)"],
            wait_ms=0,
            timeout_seconds=1,
        )
        session = self.rt.sessions[row["session_id"]]
        wait_for(lambda: session.reason == "timed_out", 4)
        self.assertIsNotNone(session.process.poll())

    def test_regex_paging_and_protected_paths(self):
        (self.root / ".env").write_text("secret-match")
        (self.root / "source.txt").write_text(
            "\n".join("value_" + str(n) for n in range(130))
        )
        row = wait_job(self.bg, self.bg.search(text=r"value_\d+$", regex=True))
        self.assertEqual(row["state"], "completed")
        self.assertEqual(row["result"]["matches"], 130)
        a = self.bg.task(row["job_id"], offset=0, limit=100)
        b = self.bg.task(row["job_id"], offset=100, limit=100)
        self.assertEqual(len(a["results"]) + len(b["results"]), 130)
        self.assertTrue(all(r["path"] == "source.txt" for r in a["results"]))
        bad = wait_job(self.bg, self.bg.search(text="[", regex=True))
        self.assertEqual(bad["state"], "error")

    def test_cancel_pathological_regex_does_not_hang_server(self):
        (self.root / "slow.txt").write_text("a" * 5000 + "!")
        row = self.bg.search(text="(a+)+$", regex=True, timeout_seconds=10)
        task = self.bg.tasks[row["job_id"]]
        wait_for(lambda: task.worker is not None)
        time.sleep(0.1)
        start = time.monotonic()
        self.bg.task(task.id, "cancel")
        wait_for(lambda: task.state != "running", 3)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(task.state, "cancelled")

    def test_regex_timeout_retains_server_responsiveness(self):
        (self.root / "slow.txt").write_text("a" * 5000 + "!")
        row = wait_job(
            self.bg, self.bg.search(text="(a+)+$", regex=True, timeout_seconds=1)
        )
        self.assertEqual(row["state"], "error")
        self.assertIn("TimeoutError", row["error"])
        terminal = self.rt.start(
            sys.executable, ["-c", "print('still-responsive')"], wait_ms=0
        )
        wait_for(
            lambda: self.rt.sessions[terminal["session_id"]].process.poll() is not None
        )

    def test_large_upload_readback_and_changed_version(self):
        block = b"large-fixture\x00" * 8192
        size = len(block) * 90
        digest = hashlib.sha256(block * 90).hexdigest()
        self.assertGreater(size, 8 * 1024 * 1024)
        row = self.bg.upload("begin", path="large.dat", size=size, sha256=digest)
        uid = row["upload_id"]
        offset = 0
        for _ in range(90):
            row = self.bg.upload(
                "write",
                upload_id=uid,
                offset=offset,
                content=base64.b64encode(block).decode(),
            )
            offset = row["next_offset"]
        self.assertFalse((self.root / "large.dat").exists())
        committed = wait_job(self.bg, self.bg.upload("commit", upload_id=uid))
        self.assertEqual(committed["state"], "completed")
        self.assertEqual(self.bg.upload("status", upload_id=uid)["state"], "completed")
        with self.assertRaises(ValueError):
            self.bg.upload("commit", upload_id=uid)
        read = self.bg.read_chunk(
            "large.dat", offset=size - len(block), limit=len(block)
        )
        self.assertEqual(base64.b64decode(read["content"]), block)
        self.assertTrue(read["eof"])
        with (self.root / "large.dat").open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(ValueError):
            self.bg.read_chunk("large.dat", expected_version=read["version"])

    def test_bad_upload_and_external_staging_mutation_never_publish(self):
        digest = hashlib.sha256(b"abcd").hexdigest()
        row = self.bg.upload("begin", path="bad.dat", size=4, sha256=digest)
        uid = row["upload_id"]
        with self.assertRaises(ValueError):
            self.bg.upload("write", upload_id=uid, offset=1, content="YWJjZA==")
        self.bg.upload("write", upload_id=uid, offset=0, content="YWJjZA==")
        self.bg.uploads[uid]["temp"].write_bytes(b"xxxx")
        row = wait_job(self.bg, self.bg.upload("commit", upload_id=uid))
        self.assertEqual(row["state"], "error")
        self.assertFalse((self.root / "bad.dat").exists())

    def test_copy_move_tree_and_concurrent_destination(self):
        src = self.root / "src"
        src.mkdir()
        (src / "empty").mkdir()
        (src / "big.bin").write_bytes(b"X" * 9 * 1024 * 1024)
        row = wait_job(
            self.bg, self.bg.file_operation("copy", source="src", destination="copy")
        )
        self.assertEqual(row["state"], "completed")
        self.assertEqual((self.root / "copy/big.bin").stat().st_size, 9 * 1024 * 1024)
        row = wait_job(
            self.bg, self.bg.file_operation("move", source="copy", destination="moved")
        )
        self.assertEqual(row["state"], "completed")
        self.assertFalse((self.root / "copy").exists())
        import udb_background

        original = udb_background.publish_new

        def raced(a, b):
            Path(b).write_text("another-owner")
            return original(a, b)

        with patch.object(udb_background, "publish_new", raced):
            row = wait_job(
                self.bg,
                self.bg.file_operation(
                    "copy", source="src/big.bin", destination="raced"
                ),
            )
        self.assertEqual(row["state"], "error")
        self.assertEqual((self.root / "raced").read_text(), "another-owner")
        self.assertFalse(list(self.root.glob(".udb-stage-*")))

    def test_tree_protected_entry_fails_without_publishing(self):
        (self.root / "src").mkdir()
        (self.root / "src/.env").write_text("protected")
        row = wait_job(
            self.bg, self.bg.file_operation("copy", source="src", destination="out")
        )
        self.assertEqual(row["state"], "error")
        self.assertFalse((self.root / "out").exists())

    def test_process_observation_rejects_self_expiry_and_pid_reuse(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
        try:
            catalog = self.rt.features.catalog
            try:
                rows = catalog.list(match="python", limit=200)["processes"]
            except RuntimeError as exc:
                if "PID namespace" in str(exc):
                    self.skipTest(str(exc))
                raise
            self.assertFalse(
                next(r for r in rows if r["pid"] == os.getpid())["can_terminate"]
            )
            child = next(r for r in rows if r["pid"] == proc.pid)
            token = child["observation_id"]
            catalog.observations[token]["until"] = 0
            with self.assertRaises(ValueError):
                catalog.terminate(token)
            self.assertIsNone(proc.poll())
            child = next(
                r
                for r in catalog.list(match="python", limit=200)["processes"]
                if r["pid"] == proc.pid
            )
            token = child["observation_id"]
            catalog.observations[token]["creation"] += 1
            with self.assertRaises(ValueError):
                catalog.terminate(token)
            self.assertIsNone(proc.poll())
            child = next(
                r
                for r in catalog.list(match="python", limit=200)["processes"]
                if r["pid"] == proc.pid
            )
            catalog.terminate(child["observation_id"])
            proc.wait(timeout=3)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
