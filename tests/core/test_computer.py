"""Synthetic access records, temp files and fake processes; never enable host access."""

import base64
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from udb import computer_access as access
from udb.computer import Computer
from udb.computer_files import MAX_FILE, Files
from udb.computer_jobs import Jobs
from udb.core import Agent, PolicyError


@pytest.fixture
def fixture(tmp_path):
    agent = Agent(tmp_path / "agent")
    work = tmp_path / "work"
    work.mkdir()
    return agent, work, Files(agent)


def record(agent):
    return dict(
        policy=1,
        id="a" * 32,
        home=str(agent.home.resolve()),
        sid="synthetic-sid",
        capabilities=list(access.CAPABILITIES),
        acknowledged_unsandboxed=True,
    )


def test_access_missing_never_reads_identity_or_creates_record(fixture):
    agent, _, _ = fixture
    with patch.object(
        access, "identity", side_effect=AssertionError("identity should not be queried")
    ):
        assert access.summary(agent)["enabled"] is False
        with pytest.raises(PolicyError, match="owner must run"):
            access.require(agent)
    assert not (agent.state / access.NAME).exists()


@pytest.mark.parametrize(
    "change",
    [
        {"policy": 2},
        {"sid": "other"},
        {"home": "wrong"},
        {"capabilities": ["files"]},
        {"acknowledged_unsandboxed": False},
        {"id": None},
    ],
)
def test_foreign_changed_or_incomplete_access_record_denied(fixture, change):
    agent, _, _ = fixture
    data = record(agent)
    data.update(change)
    (agent.state / access.NAME).write_bytes(json.dumps(data).encode())
    assert access.load(agent, unprotect=lambda b: b, sid="synthetic-sid") is None


def test_grant_reader_integrity_failure_never_echoes_payload(fixture):
    agent, _, _ = fixture
    (agent.state / access.NAME).write_bytes(b"DO-NOT-ECHO")

    def fail(_):
        raise RuntimeError("sensitive backend details")

    assert access.load(agent, unprotect=fail, sid="synthetic") is None


def test_record_reader_validates_binding_with_synthetic_backend(fixture):
    agent, _, _ = fixture
    (agent.state / access.NAME).write_bytes(json.dumps(record(agent)).encode())
    assert (
        access.load(agent, unprotect=lambda b: b, sid="synthetic-sid")["id"] == "a" * 32
    )


def test_redirected_console_cannot_create_owner_grant(fixture):
    agent, _, _ = fixture
    with pytest.raises(PolicyError, match="attended Windows terminal"):
        access.enable_local(agent, io.StringIO("ALLOW COMPUTER\n"), io.StringIO())
    assert not (agent.state / access.NAME).exists()


def test_default_redirected_stdin_never_opens_console_or_reads_permission(fixture):
    agent, _, _ = fixture
    incoming = io.StringIO("ALLOW COMPUTER\n")
    with (
        patch.object(access.sys, "stdin", incoming),
        patch.object(access.sys, "stdout", io.StringIO()),
        patch(
            "builtins.open",
            side_effect=AssertionError("Must not reopen CONIN$/CONOUT$"),
        ),
    ):
        with pytest.raises(PolicyError, match="attended Windows terminal"):
            access.enable_local(agent)
    assert incoming.tell() == 0
    assert not (agent.state / access.NAME).exists()


def test_file_binary_utf8_write_read_and_recovery(fixture):
    agent, work, files = fixture
    p = work / "tiếng Việt.txt"
    created = files.dispatch("write", str(p), content="xin chào", expected_sha256="NEW")
    assert files.dispatch("read", str(p), limit=512)["content"] == "xin chào"
    changed = files.dispatch(
        "write", str(p), content="updated", expected_sha256=created["sha256"]
    )
    assert (agent.state / "computer-recovery" / changed["recovery_id"]).read_text(
        "utf-8"
    ) == "xin chào"
    assert p.read_text() == "updated"
    binary = work / "binary.dat"
    data = b"\xff\x00\xfe"
    files.dispatch(
        "write",
        str(binary),
        content=base64.b64encode(data).decode(),
        encoding="base64",
        expected_sha256="NEW",
    )
    assert (
        base64.b64decode(
            files.dispatch("read", str(binary), encoding="base64")["content"]
        )
        == data
    )


def test_stale_sha_and_new_never_overwrite(fixture):
    _, work, files = fixture
    p = work / "original.txt"
    p.write_text("original")
    for expected in ("NEW", "0" * 64, ""):
        with pytest.raises(PolicyError):
            files.dispatch("write", str(p), content="wrong", expected_sha256=expected)
    assert p.read_text() == "original"


def test_copy_move_never_clobber_destination(fixture):
    _, work, files = fixture
    p = work / "a"
    p.write_text("a")
    expected = hashlib.sha256(b"a").hexdigest()
    q = work / "b"
    files.dispatch("copy", str(p), destination=str(q), expected_sha256=expected)
    assert p.read_text() == q.read_text() == "a"
    with pytest.raises(PolicyError):
        files.dispatch("move", str(p), destination=str(q), expected_sha256=expected)
    dst = work / "c"
    files.dispatch("move", str(p), destination=str(dst), expected_sha256=expected)
    assert dst.read_text() == "a" and not p.exists()


def test_file_api_retains_private_protected_and_link_boundaries(fixture):
    agent, work, files = fixture
    secret = work / "protected"
    secret.mkdir()
    (secret / "test").write_text("keep")
    agent.protect_local(str(secret))
    for p in (secret / "test", agent.state / "agent.sqlite3"):
        with pytest.raises(PolicyError):
            files.dispatch("read", str(p))
    p = work / "linked"
    try:
        p.symlink_to(secret / "test")
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(PolicyError):
        files.dispatch("read", str(p))


@pytest.mark.parametrize("value", ["relative/file", "../x", "", None])
def test_invalid_file_paths_rejected(fixture, value):
    with pytest.raises(PolicyError):
        fixture[2].path(value)


def test_read_ranges_and_list_limits_are_explicit(fixture):
    _, work, files = fixture
    p = work / "a"
    p.write_bytes(b"0123456789")
    result = files.dispatch("read", str(p), offset=3, limit=4)
    assert result["content"] == "3456" and result["next_offset"] == 7
    for i in range(5):
        (work / str(i)).write_text("x")
    assert files.dispatch("list", str(work), limit=2)["more"]
    with pytest.raises(PolicyError):
        files.dispatch("list", str(work), limit=501)
    with pytest.raises(PolicyError):
        files.dispatch("read", str(p), limit=0)


class FakeProcess:
    def __init__(self, argv, cwd, output):
        self.argv = argv
        self.closed = False
        self.exit = None
        output.write_bytes("synthetic output".encode())

    def poll(self):
        return self.exit

    def close(self):
        self.closed = True


@pytest.fixture
def jobs(fixture):
    agent, work, _ = fixture
    token = ["fixture"]
    manager = Jobs(agent, lambda: token[0], factory=FakeProcess)
    yield manager, token, work
    manager.close()


def until(predicate, seconds=2):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    assert predicate()


def launch(manager, work, **kw):
    return manager.start(
        str(Path(sys.executable).resolve()),
        [],
        str(work),
        kw.get("timeout", 30),
        "fixture",
    )["job_id"]


def test_hardlinked_executable_launch_is_distinct_from_data_file_access(jobs):
    manager, _, work = jobs
    original = work / "program.exe"
    original.write_bytes(b"FAKE-NOT-RUN")
    alias = work / "system-alias.exe"
    try:
        os.link(original, alias)
    except OSError:
        pytest.skip("Hard links unavailable")
    jid = manager.start(str(alias), [], str(work), 30, "fixture")["job_id"]
    assert manager.rows[jid]["process"].argv[0] == str(alias)
    with pytest.raises(PolicyError, match="Hardlinked"):
        Files(manager.agent).dispatch("read", str(alias))


def test_executable_in_explicit_protected_root_is_not_launched(jobs):
    manager, _, work = jobs
    folder = work / "protected"
    folder.mkdir()
    exe = folder / "program.exe"
    exe.write_bytes(b"FAKE")
    manager.agent.protect_local(str(folder))
    with pytest.raises(PolicyError, match="Protected root"):
        manager.start(str(exe), [], str(work), 30, "fixture")
    assert not manager.rows


def test_job_tracks_output_exit_and_never_accepts_foreign_pid(jobs):
    manager, _, work = jobs
    jid = launch(manager, work)
    assert manager.process("read", jid)["output"] == "synthetic output"
    manager.rows[jid]["process"].exit = 7
    until(lambda: manager.process("read", jid)["status"] == "exited")
    assert manager.process("read", jid)["exit_code"] == 7
    assert manager.rows[jid]["process"].closed
    with pytest.raises(PolicyError):
        manager.process("stop", str(os.getpid()))


def test_revocation_closes_only_owned_job(jobs):
    manager, token, work = jobs
    jid = launch(manager, work)
    token[0] = None
    until(lambda: manager.rows[jid]["status"] == "revoked")
    assert manager.rows[jid]["process"].closed


def test_timeout_and_output_quota_stop_jobs(jobs):
    manager, _, work = jobs
    jid = launch(manager, work)
    manager.rows[jid]["until"] = 0
    until(lambda: manager.rows[jid]["status"] == "timed_out")
    other = launch(manager, work)
    with manager.rows[other]["output"].open("wb") as stream:
        stream.truncate(4 * 1024 * 1024 + 1)
    until(lambda: manager.rows[other]["status"] == "output_limit")


def test_concurrency_limit_and_agent_shutdown(jobs):
    manager, _, work = jobs
    active = [launch(manager, work) for _ in range(10)]
    with pytest.raises(PolicyError, match="Maximum 10"):
        launch(manager, work)
    manager.close()
    assert all(manager.rows[jid]["process"].closed for jid in active)


@pytest.mark.parametrize(
    "arguments,timeout",
    [(None, 1), (["x\0"], 1), (["x"] * 129, 1), ([], 0), ([], 3601)],
)
def test_command_validation_before_process_creation(jobs, arguments, timeout):
    manager, _, work = jobs
    with pytest.raises(PolicyError):
        manager.start(
            str(Path(sys.executable).resolve()),
            arguments,
            str(work),
            timeout,
            "fixture",
        )
    assert not manager.rows


def test_computer_denies_all_real_operations_without_grant(fixture):
    agent, work, _ = fixture
    computer = Computer(agent)
    resource = dict(at=time.time(), admission_ready=True, available_mib=6000)
    try:
        assert computer.dispatch("status", {}, resource)["enabled"] is False
        for mode, args in [
            ("files", dict(action="list", path=str(work))),
            ("execute", {}),
            ("desktop", dict(action="list")),
            ("desktop", dict(action="read_text")),
            ("desktop", dict(action="set_checked", checked=True)),
            ("utilities", dict(action="apps")),
            ("process", dict(action="list")),
        ]:
            with pytest.raises(PolicyError, match="owner must run"):
                computer.dispatch(mode, args, resource)
        assert computer.desktop is None and not computer.jobs.rows
    finally:
        computer.close()


def test_desktop_rejects_foreground_change_without_injection():
    from udb.computer_desktop import Desktop

    d = Desktop.__new__(Desktop)
    d.guard = lambda: True
    d.gui = SimpleNamespace(GetForegroundWindow=lambda: 20)
    d.u = SimpleNamespace(SendInput=lambda *a: pytest.fail("Input must not be sent"))
    with pytest.raises(PolicyError, match="Foreground changed"):
        d.send(10, [d.key(65)])


def test_desktop_reports_partial_injection_and_revocation():
    from udb.computer_desktop import Desktop

    d = Desktop.__new__(Desktop)
    d.guard = lambda: True
    d.gui = SimpleNamespace(GetForegroundWindow=lambda: 10)
    d.u = SimpleNamespace(SendInput=lambda *a: 0)
    with pytest.raises(PolicyError, match="partially accepted"):
        d.send(10, [d.key(65)])
    d.guard = lambda: False
    with pytest.raises(PolicyError, match="revoked"):
        d.send(10, [d.key(65)])


def test_desktop_refuses_covered_point_before_moving_cursor_or_injecting():
    from udb.computer_desktop import Desktop

    d = Desktop.__new__(Desktop)
    d.resolve = lambda *a, **k: 10
    d.gui = SimpleNamespace(
        GetWindowRect=lambda _: (0, 0, 100, 100), WindowFromPoint=lambda _: 20
    )

    def get_ancestor(*_):
        return 20

    d.u = SimpleNamespace(GetAsyncKeyState=lambda _: 0, GetAncestor=get_ancestor)
    d.send = lambda *a: pytest.fail("Input must not be sent")

    # Function attributes emulate ctypes function metadata on non-Windows CI.
    def key_state(_):
        return 0

    d.u.GetAsyncKeyState = key_state
    with pytest.raises(PolicyError, match="covers this point"):
        d._dispatch("click", window_id="synthetic", x=50, y=50)
