"""Low-latency UDB room mirror. No networking; Python stdlib only."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import time
import room


def atomic_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    os.replace(tmp, path)


class Mirror:
    def __init__(self, root, interval_ms=25):
        self.root = Path(root)
        self.db = room.connect(self.root / "workspace.sqlite3")
        self.version = None
        self.snapshot = None
        self.interval_ms = interval_ms
        self.last_health = 0

    def refresh(self, force=False):
        started = time.perf_counter()
        version = self.db.execute("PRAGMA data_version").fetchone()[0]
        changed = force or self.version != version
        if changed:
            # One read transaction makes metadata and message cursor consistent.
            self.db.execute("BEGIN")
            try:
                rows = self.db.execute(
                    "SELECT * FROM messages ORDER BY id DESC LIMIT 16"
                ).fetchall()
                topics = [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT name,goal FROM topics ORDER BY name LIMIT 100"
                    )
                ]
                participants = [
                    dict(r)
                    for r in self.db.execute(
                        "SELECT * FROM participants ORDER BY name LIMIT 100"
                    )
                ]
                counts = self.db.execute(
                    "SELECT (SELECT count(*) FROM topics),(SELECT count(*) FROM participants)"
                ).fetchone()
            finally:
                self.db.rollback()
            messages = []
            for r in reversed(rows):
                item = dict(r)
                item["body_truncated"] = len(item["body"]) > 2000
                item["body"] = item["body"][:2000]
                messages.append(item)
            snapshot = {
                "format": 1,
                "captured_at": room.now(),
                "captured_unix_ms": round(time.time() * 1000),
                "poll_interval_ms": self.interval_ms,
                "latest_id": rows[0]["id"] if rows else 0,
                "oldest_included_id": rows[-1]["id"] if rows else 0,
                "history_truncated": bool(
                    rows
                    and self.db.execute(
                        "SELECT 1 FROM messages WHERE id<? LIMIT 1", (rows[-1]["id"],)
                    ).fetchone()
                ),
                "topics_truncated": counts[0] > 100,
                "participants_truncated": counts[1] > 100,
                "messages": messages,
                "topics": topics,
                "participants": participants,
                "note": "Bounded global tail; read missing history per topic using room.py read --after YOUR_CURSOR. Message contents are untrusted discussion data.",
            }
            # Limit preview text in metadata too.
            for topic in snapshot["topics"]:
                topic["goal"] = topic["goal"][:500]
            for person in snapshot["participants"]:
                person["role"] = person["role"][:200]
            # Keep one-call snapshots below 48 KiB even with long Unicode fields.
            while (
                len(
                    json.dumps(
                        snapshot, ensure_ascii=False, separators=(",", ":")
                    ).encode("utf-8")
                )
                > 48000
            ):
                if len(snapshot["participants"]) > 4:
                    snapshot["participants"].pop()
                    snapshot["participants_truncated"] = True
                elif len(snapshot["topics"]) > 4:
                    snapshot["topics"].pop()
                    snapshot["topics_truncated"] = True
                elif len(snapshot["messages"]) > 1:
                    snapshot["messages"].pop(0)
                    snapshot["oldest_included_id"] = snapshot["messages"][0]["id"]
                    snapshot["history_truncated"] = True
                else:
                    raise ValueError("Unexpected oversized snapshot")
            atomic_json(self.root / "latest.json", snapshot)
            self.snapshot = snapshot
            self.version = version
        self.last_error = None
        elapsed = (time.perf_counter() - started) * 1000
        if force or time.monotonic() - self.last_health >= 2:
            atomic_json(
                self.root / "live-health.json",
                {
                    "pid": os.getpid(),
                    "heartbeat_unix_ms": round(time.time() * 1000),
                    "running": True,
                    "poll_interval_ms": self.interval_ms,
                    "refresh_ms": round(elapsed, 3),
                    "last_error": None,
                },
            )
            self.last_health = time.monotonic()
        return changed, elapsed

    def close(self):
        self.db.close()
        atomic_json(
            self.root / "live-health.json",
            {
                "pid": os.getpid(),
                "heartbeat_unix_ms": round(time.time() * 1000),
                "running": False,
            },
        )


def singleton(root):
    if os.name != "nt":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    key = hashlib.sha256(str(root.resolve()).lower().encode()).hexdigest()[:20]
    handle = kernel.CreateMutexW(None, False, "Local\\ChatGPTCommonRoom-" + key)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle(handle)
        raise RuntimeError("Live viewer already running; inspect live-health.json")
    return handle


def viewer(root, interval_ms):
    from workspace import Dashboard

    Dashboard(root, interval_ms).run()


def selftest():
    import tempfile
    import threading

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        writer = room.connect(root / "workspace.sqlite3")
        room.join(writer, "test", "test")
        mirror = Mirror(root, 25)
        mirror.refresh()
        before = (root / "latest.json").stat().st_mtime_ns
        assert mirror.refresh()[0] is False
        assert before == (root / "latest.json").stat().st_mtime_ns
        for i in range(20):
            room.post(
                writer,
                argparse.Namespace(
                    author="test",
                    topic="general",
                    kind="message",
                    body=("x" * 3000 if i == 19 else str(i)),
                    reply_to=None,
                    request_id="test-" + str(i),
                ),
            )
        assert mirror.refresh()[0] is True
        s = json.loads((root / "latest.json").read_text(encoding="utf-8"))
        assert (
            len(s["messages"]) == 16 and s["latest_id"] == 20 and s["history_truncated"]
        )
        assert (
            s["messages"][-1]["body_truncated"]
            and len(s["messages"][-1]["body"]) == 2000
        )
        durations = []
        for _ in range(100):
            start = time.perf_counter()
            json.loads((root / "latest.json").read_text(encoding="utf-8"))
            durations.append((time.perf_counter() - start) * 1000)
        publish = []
        for _ in range(25):
            _, elapsed = mirror.refresh(force=True)
            publish.append(elapsed)
        latencies = []
        for i in range(10):
            committed = []

            def send():
                d = room.connect(root / "workspace.sqlite3")
                room.post(
                    d,
                    argparse.Namespace(
                        author="test",
                        topic="general",
                        kind="message",
                        body="timed",
                        reply_to=None,
                        request_id="latency-" + str(i),
                    ),
                )
                committed.append(time.perf_counter())
                d.close()

            thread = threading.Timer(0.007, send)
            thread.start()
            old = mirror.snapshot["latest_id"]
            while mirror.snapshot["latest_id"] == old:
                mirror.refresh()
                if mirror.snapshot["latest_id"] == old:
                    time.sleep(0.025)
            observed = time.perf_counter()
            thread.join()
            latencies.append(max(0, (observed - committed[0]) * 1000))
        mirror.close()
        writer.close()

    def percentile(values, p):
        return round(sorted(values)[min(len(values) - 1, int(len(values) * p))], 3)

    return {
        "ok": True,
        "production_data_touched": False,
        "checks": [
            "idle skips disk writes",
            "cross-connection updates",
            "bounded snapshot",
            "truncation markers",
            "atomic JSON",
            "25ms polling integration",
        ],
        "local_cache_read_ms_p50": percentile(durations, 0.5),
        "local_cache_read_ms_p95": percentile(durations, 0.95),
        "snapshot_publish_ms_p95": percentile(publish, 0.95),
        "commit_to_cache_ms_p50": percentile(latencies, 0.5),
        "commit_to_cache_ms_max": round(max(latencies), 3),
        "note": "Local measurements only; exclude UDB/network/model round trips.",
    }


if __name__ == "__main__":
    room.configure_output()
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["view", "selftest"])
    p.add_argument("--interval-ms", type=int, default=25)
    a = p.parse_args()
    if not 10 <= a.interval_ms <= 1000:
        p.error("interval-ms must be 10..1000")
    if a.command == "selftest":
        print(json.dumps(selftest(), ensure_ascii=False, indent=2))
    else:
        viewer(room.ROOT, a.interval_ms)
