"""One-console startup and the shared ten-job ceiling, including crashes."""

import io
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import activity, owner_command
from udb.companion.background import Background
from udb.companion.files import ScopedFiles
from udb.companion.runtime import Runtime
from udb.computer_jobs import Jobs
from udb.concurrency import MAX_CONCURRENT, TaskPool
from udb.core import Agent, PolicyError
from udb.launcher import child_environment
from udb.proxy import UnifiedProxy


def test_dispatch_accepts_ten_simultaneous_calls_without_replaying():
    proxy = UnifiedProxy(None, None)
    proxy.startup_ok = True
    barrier = threading.Barrier(11)
    replies = []
    calls = []

    def call(params):
        calls.append(params["arguments"]["index"])
        barrier.wait(timeout=10)
        return {"content": [{"type": "text", "text": "PASS | fixture"}]}

    proxy.call_tool = call
    proxy.send = replies.append
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = []
        for index in range(10):
            assert proxy.active_limit.acquire(blocking=False)
            futures.append(
                pool.submit(
                    proxy.handle,
                    {
                        "jsonrpc": "2.0",
                        "id": index,
                        "method": "tools/call",
                        "params": {"name": "fixture", "arguments": {"index": index}},
                    },
                )
            )
        assert not proxy.active_limit.acquire(blocking=False)
        barrier.wait(timeout=10)
        for future in futures:
            future.result(timeout=10)
    assert sorted(calls) == list(range(10))
    assert sorted(r["id"] for r in replies) == list(range(10))
    assert all("result" in r for r in replies)
    assert MAX_CONCURRENT == 10


def test_slots_are_shared_across_processes_and_released_after_crash(tmp_path):
    script = "from udb.concurrency import TaskPool;import sys;pool=TaskPool(sys.argv[1]);slots=[pool.claim() for _ in range(10)];print('ready',flush=True);sys.stdin.read()"
    child = subprocess.Popen(
        [sys.executable, "-u", "-c", script, str(tmp_path / "slots")],
        env=child_environment(),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pool = TaskPool(tmp_path / "slots")
    try:
        assert child.stdout.readline().strip() == "ready"
        with pytest.raises(PolicyError, match="Maximum 10"):
            pool.claim()
    finally:
        child.terminate()
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
    slots = [pool.claim() for _ in range(10)]
    for slot in slots:
        slot.close()


def test_commands_terminal_and_background_share_one_ten_job_pool(tmp_path):
    class FakeCommand:
        def __init__(self, argv, cwd, output):
            output.write_bytes(b"")
            self.closed = False

        def poll(self):
            return 0 if self.closed else None

        def close(self):
            self.closed = True

    agent = Agent(tmp_path / "owner")
    project = tmp_path / "project"
    project.mkdir()
    jobs = Jobs(agent, lambda: "fixture", factory=FakeCommand)
    rt = Runtime(str(project), True, bridge_home=agent.home)
    bg = Background(ScopedFiles(str(project)), TaskPool(agent.state / "task-slots"))
    release = threading.Event()
    try:
        for _ in range(8):
            jobs.start(
                str(Path(sys.executable).resolve()), [], str(project), 30, "fixture"
            )
        bg.new("fixture", lambda _: release.wait(20))
        terminal = rt.start(
            sys.executable,
            ["-c", "import time;time.sleep(30)"],
            wait_ms=0,
            memory_mib=128,
        )
        assert len(jobs.process("list")["jobs"]) == 8
        with pytest.raises(PolicyError, match="Maximum 10"):
            bg.new("eleventh", lambda _: pytest.fail("Must not dispatch"))
        with pytest.raises(PolicyError, match="Maximum 10"):
            jobs.start(
                str(Path(sys.executable).resolve()), [], str(project), 30, "fixture"
            )
        rt.sessions[terminal["session_id"]].stop()
        slot = TaskPool(agent.state / "task-slots").claim()
        slot.close()
    finally:
        release.set()
        rt.close()
        bg.close()
        jobs.close()
    slots = [TaskPool(agent.state / "task-slots").claim() for _ in range(10)]
    for slot in slots:
        slot.close()


def test_failed_command_creation_does_not_consume_capacity(tmp_path):
    agent = Agent(tmp_path / "owner")

    def fail(*args):
        raise OSError("fixture start failed")

    jobs = Jobs(agent, lambda: "fixture", factory=fail)
    try:
        for _ in range(12):
            with pytest.raises(OSError, match="fixture start failed"):
                jobs.start(
                    str(Path(sys.executable).resolve()),
                    [],
                    str(tmp_path),
                    30,
                    "fixture",
                )
    finally:
        jobs.close()
    slots = [TaskPool(agent.state / "task-slots").claim() for _ in range(10)]
    for slot in slots:
        slot.close()


@pytest.mark.parametrize(
    "flags,tty,follows",
    [
        ([], True, True),
        ([], False, False),
        (["--detach"], True, False),
        (["--json"], True, False),
        (["--follow"], False, True),
    ],
)
def test_start_attaches_logs_only_when_requested_or_interactive(
    tmp_path, monkeypatch, flags, tty, follows
):
    class Console(io.StringIO):
        def isatty(self):
            return tty

    report = dict(
        manager_running=True,
        local_tunnel_ready=True,
        background="ready",
        manager_mode="headless",
        next_action="fixture",
    )
    watched = []
    monkeypatch.setattr(owner_command, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(owner_command, "regular", lambda path: path)
    monkeypatch.setattr(owner_command, "executable", lambda _: tmp_path / "udb.exe")
    monkeypatch.setattr(owner_command, "start", lambda *a, **kw: report)
    monkeypatch.setattr(owner_command.sys, "stdin", Console())
    output = Console()
    monkeypatch.setattr(owner_command.sys, "stdout", output)
    monkeypatch.setattr(
        activity, "watch", lambda *a, **kw: watched.append((a, kw)) or 0
    )
    assert owner_command.main(["start", "--home", str(tmp_path), *flags]) == 0
    assert bool(watched) == follows
    if "--json" in flags:
        assert json.loads(output.getvalue())["manager_running"] is True
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "state,symbol", [("CALL", "📞"), ("PASS", "✅"), ("ERROR", "❌"), ("RUNNING", "⏳")]
)
def test_readable_statuses_have_emojis_without_changing_json_fields(state, symbol):
    row = dict(
        at=0,
        kind="call" if state == "CALL" else "result",
        status=state,
        tool="fixture",
        call_id="a",
        elapsed_ms=1,
    )
    assert symbol in activity.render_event(row)
    assert symbol not in activity.render_event(row, emoji=False)


def test_ascii_terminal_keeps_working_with_emoji_fallback(tmp_path):
    class ASCIIConsole(io.StringIO):
        @property
        def encoding(self):
            return "ascii"

        def write(self, value):
            value.encode("ascii")
            return super().write(value)

    activity.Activity(tmp_path).emit("call", "fixture")
    output = ASCIIConsole()
    assert activity.watch(tmp_path, once=True, output=output) == 0
    assert "CALL" in output.getvalue()
