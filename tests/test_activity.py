"""Live logging must preserve MCP framing, dispatch and private payloads."""

import io
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from udb import activity, cli
from udb.activity import Activity, read_events
from udb.launcher import command
from udb.proxy import UnifiedProxy
from udb.transport import StdioChild


def test_real_mcp_logging_keeps_stdio_parseable_and_does_not_store_payloads(tmp_path):
    home = tmp_path / "owner"
    project = tmp_path / "project"
    home.mkdir()
    project.mkdir()
    child = StdioChild(
        "activity fixture",
        command(["serve", "--home", str(home), "--root", str(project)]),
        str(tmp_path),
        lambda _: None,
    )
    child.start()
    try:
        assert "result" in child.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "activity test", "version": "1"},
            },
        )
        child.notification({"jsonrpc": "2.0", "method": "notifications/initialized"})
        response = child.request(
            "tools/call", {"name": "bridge_status", "arguments": {}}
        )
        assert response["result"].get("isError") is not True
        denied = child.request(
            "tools/call",
            {
                "name": "missing_fixture_tool",
                "arguments": {
                    "text": "PRIVATE_FILE_CONTENT",
                    "api_key": "SECRET_CREDENTIAL",
                },
            },
        )
        assert "error" in denied
        deadline = time.monotonic() + 5
        while len(read_events(home)) < 4 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(read_events(home)) == 4
    finally:
        child.close()
    rows = read_events(home)
    assert [r["kind"] for r in rows] == ["call", "result", "call", "result"]
    assert [r["status"] for r in rows if r["kind"] == "result"] == ["PASS", "ERROR"]
    assert rows[0]["call_id"] == rows[1]["call_id"]
    assert rows[2]["call_id"] == rows[3]["call_id"] != rows[0]["call_id"]
    serialized = json.dumps(rows)
    assert (
        "PRIVATE_FILE_CONTENT" not in serialized
        and "SECRET_CREDENTIAL" not in serialized
    )
    assert "text_hidden" in serialized
    assert not (home / "state/computer-access.dpapi").exists()


@pytest.mark.parametrize("raises", [False, True])
def test_failed_observer_never_changes_dispatch_or_replays_calls(raises, capsys):
    class FailedObserver:
        def emit(self, *args):
            raise OSError("SENSITIVE_OBSERVER_ERROR")

    proxy = UnifiedProxy(None, None, FailedObserver())
    proxy.startup_ok = True
    calls = []
    replies = []

    def call(params):
        calls.append(params)
        if raises:
            raise TimeoutError("outcome unknown")
        return {"content": [{"type": "text", "text": "RUNNING | pending"}]}

    proxy.call_tool = call
    proxy.send = replies.append
    assert proxy.active_limit.acquire()
    proxy.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "fixture", "arguments": {}},
        }
    )
    assert len(calls) == len(replies) == 1
    assert ("error" in replies[0]) == raises
    assert proxy.active_limit.acquire(timeout=0.1)
    assert capsys.readouterr() == ("", "")


def test_retention_and_concurrent_proxies_use_one_ordered_log(tmp_path, monkeypatch):
    monkeypatch.setattr(activity, "KEEP_ROWS", 20)
    first, second = Activity(tmp_path), Activity(tmp_path)
    first.emit("call", "seed")
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(
            pool.map(
                lambda i: (first if i % 2 else second).emit(
                    "call", "fixture", str(i), details={"offset": i}
                ),
                range(40),
            )
        )
    rows = read_events(tmp_path, limit=100)
    assert len(rows) == 20
    assert [r["seq"] for r in rows] == list(range(22, 42))
    assert all(r["tool"] == "fixture" for r in rows)
    assert read_events(tmp_path, after=rows[-2]["seq"]) == rows[-1:]


@pytest.mark.parametrize(
    "report,native,expected",
    [
        ({"status": "running"}, "PASS", "RUNNING"),
        ({"exit_code": 2}, "PASS", "ERROR"),
        ({"status": "completed"}, "RUNNING", "RUNNING"),
        ({"status": "completed"}, "ERROR", "ERROR"),
    ],
)
def test_pending_and_failure_are_never_logged_as_success(report, native, expected):
    result = {
        "structuredContent": report,
        "content": [{"type": "text", "text": native + " | fixture"}],
    }
    assert activity.completion(result)[0] == expected


def test_cli_snapshot_is_read_only_and_json_stays_json(tmp_path, capsys):
    home = tmp_path / "not-created"
    assert cli.main(["logs", "--home", str(home), "--once", "--json"]) == 0
    assert not home.exists()
    assert capsys.readouterr().out == ""
    Activity(home).emit("call", "file_read", "a", details={"path": "hello.py"})
    assert cli.main(["logs", "--home", str(home), "--once", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tool"] == "file_read"


def test_follow_from_now_streams_new_calls_and_ctrl_c_only_closes_viewer(
    tmp_path, monkeypatch
):
    log = Activity(tmp_path)
    log.emit("call", "old")
    sleeps = []

    def tick(_):
        if sleeps:
            raise KeyboardInterrupt
        sleeps.append(True)
        log.emit("call", "new", "b")

    monkeypatch.setattr(activity.time, "sleep", tick)
    output = io.StringIO()
    assert activity.watch(tmp_path, tail=0, output=output) == 0
    assert " new #b" in output.getvalue() and " old " not in output.getvalue()
    assert len(read_events(tmp_path)) == 2


def test_metadata_omits_command_bodies_and_terminal_escape_sequences(tmp_path):
    details = activity.argument_details(
        {
            "program": "python.exe",
            "arguments": ["-c", "SECRET_COMMAND"],
            "content": "PRIVATE_SOURCE",
            "text": "PRIVATE_TEXT",
            "api_key": "SECRET_KEY",
            "path": "sk-secret123456\x1b\n\u202etest.py",
        }
    )
    Activity(tmp_path).emit(
        "call", "tool\x1b\n", details={**details, "password": "SECRET_PASSWORD"}
    )
    rendered = activity.render_event(read_events(tmp_path)[0], details=True)
    assert all(
        x not in rendered
        for x in [
            "SECRET_COMMAND",
            "PRIVATE_SOURCE",
            "PRIVATE_TEXT",
            "SECRET_KEY",
            "SECRET_PASSWORD",
            "sk-secret123456",
            "\x1b",
            "\u202e",
        ]
    )
    assert "arguments_count" in rendered and "content_hidden" in rendered


def test_linked_state_is_not_followed_and_broken_sqlite_cannot_fail_a_tool(tmp_path):
    real = tmp_path / "outside"
    home = tmp_path / "home"
    real.mkdir()
    home.mkdir()
    try:
        (home / "state").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("Local symlink permission unavailable")
    Activity(home).emit("call", "fixture")
    assert not list(real.iterdir())
    with pytest.raises(RuntimeError):
        read_events(home)


def test_malformed_database_reports_generic_viewer_error(tmp_path, capsys):
    path = tmp_path / "state/activity.sqlite3"
    path.parent.mkdir()
    path.write_bytes(b"PRIVATE_INVALID_DATABASE")
    Activity(tmp_path).emit("call", "fixture")
    assert activity.watch(tmp_path, once=True, as_json=True) == 2
    assert "PRIVATE_INVALID_DATABASE" not in capsys.readouterr().err


def test_reader_can_start_while_database_is_being_initialized(tmp_path):
    path = tmp_path / "state/activity.sqlite3"
    path.parent.mkdir()
    with sqlite3.connect(path):
        assert read_events(tmp_path) == []
