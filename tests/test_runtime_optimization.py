"""Bounded checkpoint memory, log resets and owned process shutdown races."""

import collections
import io
import sys
import threading
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from udb import activity, headless
from udb.companion import runtime
from udb.companion.terminal import retained_tail
from udb.concurrency import TaskPool


@pytest.mark.parametrize("limit", [0, 1, 4096, 65536, 2**20])
def test_checkpoint_tail_preserves_exact_fragmented_bytes(limit):
    pieces = [b"", b"a", "Việt Nam".encode() * 777, bytes(range(256)) * 600]
    chunks = collections.deque(enumerate(pieces))
    expected = b"".join(pieces)
    assert retained_tail(chunks, limit) == (expected[-limit:] if limit else b"")


def test_checkpoint_tail_does_not_copy_the_whole_retained_megabyte():
    chunks = collections.deque((i * 4096, b"x" * 4096) for i in range(256))
    tracemalloc.start()
    try:
        result = retained_tail(chunks)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result == b"x" * 65536
    assert peak < 200000


def test_close_joins_monitor_and_refuses_later_process_creation(tmp_path, monkeypatch):
    rt = runtime.Runtime(str(tmp_path), enable_terminal=True)
    rt.close()
    assert not rt.monitor.is_alive()
    launched = []
    monkeypatch.setattr(runtime, "ManagedProcess", lambda *a, **kw: launched.append(a))
    with pytest.raises(RuntimeError, match="closed"):
        rt.start(sys.executable)
    assert launched == []


def test_close_during_process_creation_does_not_leave_a_process_or_slot(
    tmp_path, monkeypatch
):
    rt = runtime.Runtime(str(tmp_path), enable_terminal=True)
    creating = threading.Event()
    release = threading.Event()
    original = runtime.ManagedProcess

    def blocked(*args, **kwargs):
        creating.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime, "ManagedProcess", blocked)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            start = pool.submit(
                rt.start,
                sys.executable,
                ["-c", "import time;time.sleep(30)"],
                wait_ms=0,
            )
            assert creating.wait(5)
            close = pool.submit(rt.close)
            try:
                assert rt.done.wait(5)
            finally:
                release.set()
            start.result(timeout=10)
            close.result(timeout=10)
        assert not rt.monitor.is_alive()
        assert rt.sessions and all(
            s.process.poll() is not None for s in rt.sessions.values()
        )
        slots = [
            TaskPool(tmp_path / ".udb-work-state/task-slots").claim() for _ in range(10)
        ]
        for slot in slots:
            slot.close()
    finally:
        release.set()
        rt.close()


def test_follow_recovers_when_database_sequence_restarts(tmp_path, monkeypatch):
    log = activity.Activity(tmp_path)
    for _ in range(4):
        log.emit("call", "old")
    ticks = []

    def tick(_):
        if ticks:
            raise KeyboardInterrupt
        ticks.append(True)
        activity.activity_path(tmp_path).unlink()
        log.emit("call", "after_reset")

    monkeypatch.setattr(activity.time, "sleep", tick)
    output = io.StringIO()
    assert activity.watch(tmp_path, tail=0, output=output) == 0
    assert "after_reset" in output.getvalue()
    assert " old " not in output.getvalue()


def test_heartbeat_reduces_writes_without_slowing_local_stop_checks(monkeypatch):
    clock = [0.0]
    writes = []
    checks = []

    class Event:
        def is_set(self):
            return clock[0] >= 3.25

        def wait(self, interval):
            clock[0] += interval

    ctl = SimpleNamespace(
        agent=SimpleNamespace(state="unused"),
        start_host=lambda: None,
        close=lambda: None,
    )
    from pathlib import Path

    ctl.agent.state = Path("unused")
    monkeypatch.setattr(headless, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(headless, "publish", lambda *a: writes.append(clock[0]))
    monkeypatch.setattr(
        headless, "stop_requested", lambda *a: checks.append(clock[0]) or False
    )
    headless.run_loop(ctl, "fixture", Event(), connect=False, interval=0.1)
    assert len(checks) >= 32
    assert 3 <= len(writes) - 1 <= 4
    assert max(b - a for a, b in zip(writes, writes[1:], strict=False)) <= 1.2


def test_close_joins_monitor_even_when_checkpoint_fails(tmp_path, monkeypatch):
    rt = runtime.Runtime(str(tmp_path))

    def fail():
        raise OSError("synthetic checkpoint failure")

    with monkeypatch.context() as m:
        m.setattr(rt, "checkpoint", fail)
        with pytest.raises(OSError, match="synthetic"):
            rt.close()
    assert not rt.monitor.is_alive()
    rt.close()
