"""Regression cases for standalone independence and truthful RPC outcomes."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest

from udb import owner_command, protocol
from udb.computer_files import Files
from udb.core import Agent, PolicyError
from udb.proxy import normalized_output
from udb.transport import StdioChild, compact_json


@pytest.mark.parametrize(
    "metadata", ["not-json", json.dumps({"active": "missing", "sha256": "0" * 64})]
)
def test_standalone_lifecycle_does_not_require_a_legacy_installation(
    tmp_path, monkeypatch, metadata
):
    home = tmp_path / "existing-home"
    home.mkdir()
    (home / "installation.json").write_text(metadata)
    executable = tmp_path / "standalone.exe"
    executable.write_bytes(b"synthetic executable")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    assert owner_command.executable(home) == executable
    assert (home / "installation.json").read_text() == metadata


@pytest.mark.parametrize("action", ["copy", "move"])
def test_computer_copy_does_not_publish_bytes_changed_after_sha_check(
    tmp_path, monkeypatch, action
):
    files = Files(Agent(tmp_path / "owner"))
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_bytes(b"reviewed bytes")
    expected = hashlib.sha256(source.read_bytes()).hexdigest()
    original = files.check_expected
    changed = False

    def checked_then_changed(path, digest):
        nonlocal changed
        original(path, digest)
        if path == source and not changed:
            changed = True
            source.write_bytes(b"concurrent external edit")

    monkeypatch.setattr(files, "check_expected", checked_then_changed)
    with pytest.raises(PolicyError, match="changed|SHA256"):
        files.dispatch(
            action, str(source), destination=str(destination), expected_sha256=expected
        )
    assert not destination.exists()
    assert source.read_bytes() == b"concurrent external edit"


@pytest.mark.parametrize(
    "state,expected",
    [
        ("cancelled", "ERROR"),
        ("canceled", "ERROR"),
        ("interrupted", "ERROR"),
        ("error", "ERROR"),
        ("ERROR", "ERROR"),
        ("failed", "ERROR"),
        ("owned_by_live_backend", "RUNNING"),
        ("running", "RUNNING"),
        ("RUNNING", "RUNNING"),
        ("completed", "PASS"),
    ],
)
def test_job_result_preserves_failure_and_live_ownership(state, expected):
    row = protocol.result({"state": state})
    assert row["content"][0]["text"].startswith(expected + " |")
    assert row["isError"] is (expected == "ERROR")
    assert row["_meta"]["udb"]["status"] == expected


def test_backend_error_flag_overrides_a_stale_pass_label():
    row = normalized_output(
        "fixture",
        {"isError": True, "content": [{"type": "text", "text": "PASS | stale label"}]},
    )
    assert row["isError"] is True
    assert row["content"][0]["text"].startswith("ERROR |")


class ShortWriter:
    def __init__(self, count):
        self.count = count
        self.bytes = bytearray()
        self.flushed = False

    def write(self, value):
        part = bytes(value[: self.count])
        self.bytes.extend(part)
        return len(part)

    def flush(self):
        self.flushed = True


def test_rpc_pipe_short_writes_still_send_one_complete_utf8_frame():
    writer = ShortWriter(3)
    child = StdioChild("fixture", [], ".", lambda _: None)
    child.process = SimpleNamespace(stdin=writer)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "fixture",
        "params": {"text": "Tiếng Việt"},
    }
    child._send(message)
    assert writer.bytes == compact_json(message)
    assert writer.flushed


def test_rpc_pipe_closed_during_write_reports_failure():
    writer = ShortWriter(0)
    child = StdioChild("fixture", [], ".", lambda _: None)
    child.process = SimpleNamespace(stdin=writer)
    with pytest.raises(RuntimeError, match="write failed"):
        child._send({"jsonrpc": "2.0", "id": 1, "method": "fixture"})


def test_failing_notification_observer_does_not_disconnect_backend(tmp_path):
    import threading

    observed = []

    def failing_observer(message):
        observed.append(message)
        raise RuntimeError("observer unavailable")

    child = StdioChild("fixture", [], str(tmp_path), failing_observer)
    messages = [
        {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}},
        {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}},
    ]
    stream = SimpleNamespace(
        readline=lambda limit: compact_json(messages.pop(0)) if messages else b""
    )
    child.process = SimpleNamespace(stdout=stream)
    waiter = __import__("queue").Queue(maxsize=1)
    child.waiters[7] = waiter

    thread = threading.Thread(target=child._read_loop)
    thread.start()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(observed) == 1
    assert waiter.get_nowait()["result"] == {"ok": True}
    assert child.terminated.is_set()


@pytest.mark.parametrize("encoding", ["json", "wrapped_json", "structured"])
@pytest.mark.parametrize(
    "report,expected",
    [
        ({"state": "interrupted"}, "ERROR"),
        ({"state": "owned_by_live_backend"}, "RUNNING"),
        ({"state": "completed", "exit_code": 3}, "ERROR"),
        ({"ok": False, "error": "fixture failure"}, "ERROR"),
        ({"state": "completed", "exit_code": 0}, "PASS"),
    ],
)
def test_report_encodings_have_the_same_public_outcome(report, expected, encoding):
    text = json.dumps(report)
    if encoding == "wrapped_json":
        text = json.dumps(text)
    original = {"content": [{"type": "text", "text": text}]}
    if encoding == "structured":
        original = {
            "content": [{"type": "text", "text": "Fixture result"}],
            "structuredContent": report,
        }
    original_json = json.dumps(original)
    row = normalized_output("fixture", original)
    assert row["content"][0]["text"].startswith(expected + " |")
    assert protocol.result(report)["_meta"]["udb"]["status"] == expected
    assert bool(row.get("isError")) is (expected == "ERROR")
    assert json.dumps(original) == original_json


def test_error_heading_cannot_be_annotated_as_success():
    row = protocol.annotate({"content": [{"type": "text", "text": "ERROR | fixture"}]})
    assert row["_meta"]["udb"]["status"] == "ERROR"


def test_terminal_checkpoint_skips_unchanged_snapshots():
    import collections
    import threading

    from udb.companion.runtime import Runtime

    process = SimpleNamespace(returncode=None)
    session = SimpleNamespace(
        id="a" * 12,
        lock=threading.Lock(),
        chunks=collections.deque([(0, b"hello")]),
        position=5,
        done=False,
        reason="",
        mode="pipe",
        process=SimpleNamespace(poll=lambda: process.returncode),
    )
    writes = []
    runtime = Runtime.__new__(Runtime)
    runtime.features = SimpleNamespace(
        background=SimpleNamespace(checkpoint=lambda: None)
    )
    runtime.sessions = {session.id: session}
    runtime.journal = SimpleNamespace(save=lambda key, row: writes.append((key, row)))
    runtime._terminal_checkpoint_signatures = {}

    runtime.checkpoint()
    runtime.checkpoint()
    assert len(writes) == 1

    with session.lock:
        session.chunks.append((session.position, b"!"))
        session.position += 1
    runtime.checkpoint()
    assert len(writes) == 2
    assert writes[-1][1]["next_offset"] == 6

    session.done = True
    process.returncode = 0
    runtime.checkpoint()
    assert len(writes) == 3
    assert writes[-1][1]["state"] == "finished"
