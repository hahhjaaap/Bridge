"""Streaming file limits, read-only polling and bounded idle worker costs."""

import base64
import hashlib
import json
import os
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import host_service
from udb.computer_files import MAX_FILE, Files
from udb.core import Agent, PolicyError


@pytest.mark.parametrize(
    "offset,limit", [(0, 200), (262140, 64), (600000, 100000), (900000, 200)]
)
def test_range_read_hashes_the_whole_file_and_returns_only_requested_bytes(
    tmp_path, offset, limit
):
    data = bytes(range(256)) * 3000
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    files = Files(Agent(tmp_path / "owner"))
    result = files.dispatch(
        "read", str(source), encoding="base64", offset=offset, limit=limit
    )
    expected = data[offset : offset + limit]
    assert base64.b64decode(result["content"]) == expected
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert result["bytes"] == len(data)
    assert result["next_offset"] == (
        offset + len(expected) if offset + len(expected) < len(data) else None
    )


@pytest.mark.parametrize("operation", ["digest", "read", "copy"])
def test_large_file_operations_keep_memory_bounded(tmp_path, operation):
    files = Files(Agent(tmp_path / "owner"))
    source = tmp_path / "source.bin"
    source.write_bytes(b"x" * MAX_FILE)
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    tracemalloc.start()
    try:
        if operation == "digest":
            assert files.digest(source) == expected
        elif operation == "read":
            result = files.dispatch("read", str(source), limit=200)
            assert result["content"] == "x" * 200 and result["sha256"] == expected
        else:
            destination = tmp_path / "copy.bin"
            result = files.dispatch(
                "copy",
                str(source),
                destination=str(destination),
                expected_sha256=expected,
            )
            assert result["sha256"] == expected
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 2 * 1024 * 1024
    if operation == "copy":
        assert destination.read_bytes() == source.read_bytes()


def test_streaming_copy_rejects_growth_beyond_file_limit(tmp_path, monkeypatch):
    files = Files(Agent(tmp_path / "owner"))
    source, destination = tmp_path / "source.bin", tmp_path / "copy.bin"
    source.write_bytes(b"reviewed")
    expected = files.digest(source)
    scan = files._scan

    def changed(p, *args, **kwargs):
        if kwargs.get("sink") is not None:
            source.write_bytes(b"x" * (MAX_FILE + 1))
        return scan(p, *args, **kwargs)

    monkeypatch.setattr(files, "_scan", changed)
    with pytest.raises(PolicyError, match="8 MiB"):
        files.dispatch(
            "copy", str(source), destination=str(destination), expected_sha256=expected
        )
    assert not destination.exists()
    assert not list(tmp_path.glob(".udb-*.tmp"))


def test_streaming_copy_never_replaces_a_concurrent_destination(tmp_path, monkeypatch):
    files = Files(Agent(tmp_path / "owner"))
    source, destination = tmp_path / "source.bin", tmp_path / "copy.bin"
    source.write_bytes(b"reviewed")
    expected = files.digest(source)
    scan = files._scan

    def competing_writer(p, *args, **kwargs):
        result = scan(p, *args, **kwargs)
        if kwargs.get("sink") is not None:
            destination.write_bytes(b"external edit")
        return result

    monkeypatch.setattr(files, "_scan", competing_writer)
    with pytest.raises(PolicyError, match="already exists"):
        files.dispatch(
            "copy", str(source), destination=str(destination), expected_sha256=expected
        )
    assert destination.read_bytes() == b"external edit"
    assert not list(tmp_path.glob(".udb-*.tmp"))


def test_idle_approval_poll_can_read_during_another_writer_transaction(
    tmp_path, monkeypatch
):
    agent = Agent(tmp_path)
    connect = agent.connect

    def short_timeout():
        db = connect()
        db.execute("PRAGMA busy_timeout=50")
        return db

    monkeypatch.setattr(agent, "connect", short_timeout)
    with agent.connect() as db:
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
            ("a" * 32, "write", "{}", "", time.time(), "pending", "{}"),
        )
    with agent.connect() as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert [r["id"] for r in agent.pending_local()] == ["a" * 32]


def test_expired_approval_and_autonomy_queues_are_still_expired(tmp_path):
    agent = Agent(tmp_path)
    with agent.connect() as db:
        for index, status in enumerate(
            ("pending", "queued_sandbox", "queued_media", "applied")
        ):
            db.execute(
                "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
                (str(index) * 32, "write", "{}", "", time.time() - 601, status, "{}"),
            )
    assert agent.pending_local() == []
    with agent.connect() as db:
        assert [
            r[0] for r in db.execute("SELECT status FROM requests ORDER BY id")
        ] == ["expired", "expired", "expired", "applied"]


def test_job_tail_keeps_normal_offset_reads_and_reports_actual_end(tmp_path):
    agent = Agent(tmp_path)
    jid = "b" * 32
    with agent.connect() as db:
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?)",
            (jid, "running", json.dumps({"language": "python"})),
        )
    folder = agent.state / "jobs" / jid / "output"
    folder.mkdir(parents=True)
    data = b"start" + b"x" * 100000 + b"recent output"
    (folder / "output.log").write_bytes(data)
    assert agent.job_status(jid, 0, 5)["output"] == "start"
    recent = agent.job_status(jid, limit=13, tail=True)
    assert recent["output"] == "recent output"
    assert recent["next_offset"] == len(data)


def test_idle_host_worker_backs_off_and_prunes_replies_on_a_separate_schedule(
    tmp_path, monkeypatch
):
    clock, waits, cleanups = [0.0], [], []

    class Stop:
        def is_set(self):
            return clock[0] >= 9

        def wait(self, duration):
            waits.append(duration)
            clock[0] += duration

    service = host_service.HostService.__new__(host_service.HostService)
    service.folder = tmp_path
    (tmp_path / "inbox").mkdir()
    service.stop_event = Stop()
    service.maintenance = threading.Event()
    service._prune_replies = lambda: cleanups.append(clock[0])
    monkeypatch.setattr(
        host_service, "time", SimpleNamespace(monotonic=lambda: clock[0])
    )
    service._work()
    assert len(waits) < 45
    assert max(waits) <= 0.25
    assert len(cleanups) == 2


def test_consumed_reply_does_not_abort_pruning_other_expired_replies(
    tmp_path, monkeypatch
):
    folder = tmp_path / "replies"
    folder.mkdir()
    expired, recent = folder / "expired.json", folder / "recent.json"
    expired.write_text("{}")
    recent.write_text("{}")
    os.utime(expired, (0, 0))
    monkeypatch.setattr(
        Path, "glob", lambda *args: iter([folder / "consumed.json", expired, recent])
    )
    service = host_service.HostService.__new__(host_service.HostService)
    service.folder = tmp_path
    service._prune_replies()
    assert not expired.exists() and recent.exists()
