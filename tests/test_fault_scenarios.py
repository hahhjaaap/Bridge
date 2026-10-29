"""Fault injection around host RPC, ownership and shutdown; disposable state only."""

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import host_client, protocol
from udb.computer_jobs import Jobs
from udb.core import Agent, PolicyError
from udb.desktop import DesktopController
from udb.desktop_control import browser_handoff
from udb.host_service import HostService


def health(**changes):
    result = dict(
        worker_ready=True,
        resources=dict(at=time.time(), admission_ready=True),
        sessions=[],
    )
    result.update(changes)
    return result


@pytest.fixture
def owner(tmp_path):
    agent = Agent(tmp_path / "owner")
    (agent.state / "host/inbox").mkdir(parents=True)
    (agent.state / "host/replies").mkdir()
    host_client.atomic_json(agent.state / "host-settings.json", {"enabled": True})
    host_client.atomic_json(agent.state / "host/status.json", health())
    return agent


@pytest.mark.parametrize(
    "changes",
    [
        {"worker_ready": "false"},
        {"sessions": None},
        {"sessions": [None]},
        {"sessions": [{"session_id": "a" * 32, "kind": "browser", "tabs": "two"}]},
        {"resources": {"at": "now", "admission_ready": True}},
        {"resources": {"at": float("nan"), "admission_ready": True}},
        pytest.param(
            lambda: {"resources": {"at": time.time(), "admission_ready": "false"}},
            id="invalid-admission-type",
        ),
    ],
)
def test_malformed_health_never_reports_a_ready_worker(owner, changes):
    if callable(changes):
        changes = changes()
    host_client.atomic_json(owner.state / "host/status.json", health(**changes))
    result = host_client.status(owner)
    assert result["worker_ready"] is False
    assert result["sessions"] == []


@pytest.mark.parametrize(
    "reply", [[], None, {"ok": "false", "result": {}}, {"ok": True}]
)
def test_malformed_reply_reports_an_unknown_outcome_without_retry(
    owner, monkeypatch, reply
):
    original = host_client.atomic_json
    calls = []

    def respond(path, payload):
        calls.append(payload)
        original(path, payload)
        original(owner.state / "host/replies" / path.name, reply)

    monkeypatch.setattr(host_client, "atomic_json", respond)
    with pytest.raises(PolicyError, match="reply|outcome|inspect"):
        host_client.command(owner, "start", kind="browser", task="fixture")
    assert len(calls) == 1


@pytest.mark.parametrize("jump", [-3600, 3600])
def test_rpc_wait_budget_uses_elapsed_time_when_system_clock_changes(
    owner, monkeypatch, jump
):
    elapsed = [0.0]
    monkeypatch.setattr(
        host_client, "status", lambda agent: {"worker_ready": True, "enabled": True}
    )

    def sleep(_):
        elapsed[0] += 1
        if elapsed[0] > 30:
            raise AssertionError("RPC remained blocked after its elapsed-time budget")

    monkeypatch.setattr(
        host_client,
        "time",
        SimpleNamespace(
            time=lambda: 1000000 + (jump if elapsed[0] else 0),
            monotonic=lambda: elapsed[0],
            sleep=sleep,
        ),
    )
    with pytest.raises(PolicyError, match="timed out"):
        host_client.command(owner, "start", kind="browser", task="fixture")
    assert 27 <= elapsed[0] <= 28
    assert len(list((owner.state / "host/inbox").glob("*.json"))) == 1


@pytest.fixture
def service(owner):
    engine = HostService.__new__(HostService)
    engine.agent = owner
    engine.folder = owner.state / "host"
    engine.sessions = {}
    engine.resource = {
        "at": time.time(),
        "admission_ready": True,
        "available_mib": 8000,
    }
    engine.current = None
    engine.lock = threading.RLock()
    engine.maintenance = threading.Event()
    engine.stop_event = threading.Event()
    engine.publish = lambda: None
    return engine


@pytest.mark.parametrize("failure", ["blocked_link", "unreadable_entry", "directory"])
def test_one_bad_mailbox_entry_does_not_delay_later_valid_commands(
    service, monkeypatch, failure
):
    import udb.host_service as module

    poison = service.folder / "inbox" / ("0" * 32 + ".json")
    if failure == "directory":
        poison.mkdir()
    else:
        poison.write_text("{}")
    if failure == "blocked_link":
        original = module.reject_links

        def reject(path):
            if path == poison:
                raise PolicyError("Simulated linked command")
            return original(path)

        monkeypatch.setattr(module, "reject_links", reject)
    elif failure == "unreadable_entry":
        original_stat = Path.stat

        def unavailable(path, *args, **kwargs):
            if path == poison:
                raise PermissionError("Simulated locked command")
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", unavailable)
    valid = service.folder / "inbox" / ("f" * 32 + ".json")
    host_client.atomic_json(
        valid, dict(id=valid.stem, expires=time.time() + 25, op="stop_all", args={})
    )
    calls = []
    service.dispatch = lambda op, args: calls.append(op) or {"stopped": True}

    class SingleScan(threading.Event):
        def wait(self, timeout=None):
            self.set()
            return True

    service.stop_event = SingleScan()
    service._work()
    assert calls == ["stop_all"]
    reply = json.loads((service.folder / "replies" / valid.name).read_text())
    assert reply["ok"] is True


class Session:
    def __init__(self, failures=0, visible=False):
        self.failures = failures
        self.visible = visible
        self.closed = False
        self.tabs = {}
        self.proc = SimpleNamespace(alive=lambda: not self.closed, usage=lambda: {})

    def close(self):
        if self.failures:
            self.failures -= 1
            raise OSError("Simulated process handle error")
        self.closed = True
        self.proc = None


def add_session(service, sid, obj, kind="browser"):
    service.sessions[sid] = dict(
        kind=kind, task="fixture", object=obj, expires=time.time() + 300
    )


def test_failed_close_keeps_owned_session_for_cleanup_and_blocks_new_actions(service):
    sid, session = "a" * 32, Session(failures=1, visible=True)
    add_session(service, sid, session)
    host_client.atomic_json(
        service.agent.state / "browser-handoff.json", {"active": True}
    )
    with pytest.raises(OSError):
        service._close(sid)
    assert sid in service.sessions
    assert browser_handoff(service.agent)
    with pytest.raises(PolicyError, match="closing"):
        service._session(sid)
    assert service._close(sid)["closed"]
    assert not service.sessions and not browser_handoff(service.agent)


def test_stop_all_attempts_healthy_sessions_even_when_another_close_fails(service):
    failed, healthy = Session(failures=1), Session()
    add_session(service, "a" * 32, failed)
    add_session(service, "b" * 32, healthy, "workspace")
    with pytest.raises((OSError, PolicyError)):
        service.dispatch("stop_all", {})
    assert healthy.closed
    assert "a" * 32 in service.sessions
    service._maintain()
    assert failed.closed and not service.sessions


def test_failed_browser_handoff_does_not_leave_desktop_input_blocked(service):
    session = Session()

    def failed(_):
        raise PolicyError("Simulated browser restart failure")

    session.switch_presentation = failed
    add_session(service, "a" * 32, session)
    with pytest.raises(PolicyError, match="browser restart failure"):
        service.dispatch("browser_presentation", {"visible": True})
    assert not browser_handoff(service.agent)


def test_controller_shutdown_still_stops_other_children_after_host_failure(
    tmp_path, monkeypatch
):
    controller = DesktopController(tmp_path, monitor=False)
    attempts = []

    class Host:
        def close(self):
            attempts.append("host")
            raise OSError("Simulated host close failure")

    controller.host_service = Host()
    controller.children = {"tunnel": object(), "mcp": object()}
    monkeypatch.setattr(controller, "stop_process", lambda name: attempts.append(name))
    with pytest.raises((OSError, PolicyError)):
        controller.close()
    assert attempts == ["host", "tunnel", "mcp"]
    controller.host_service = None
    controller.children.clear()
    controller.close()


def bare_jobs(rows):
    jobs = Jobs.__new__(Jobs)
    jobs.rows = rows
    jobs.lock = threading.RLock()
    jobs.done = threading.Event()
    jobs.thread = SimpleNamespace(join=lambda **kwargs: None)
    return jobs


def job_row(session):
    return dict(
        status="running",
        exit_code=None,
        process=session,
        grant="fixture",
        until=time.monotonic() + 60,
    )


def test_failed_job_stop_can_be_retried_without_restarting_the_command():
    session = Session(failures=1)
    jobs = bare_jobs({"owned": job_row(session)})
    assert jobs.process("stop", "owned")["status"] == "cleanup_failed"
    assert jobs.process("stop", "owned")["status"] == "stopped"
    assert session.closed


def test_job_shutdown_reports_retained_cleanup_failure_after_attempting_other_jobs():
    failed, healthy = Session(failures=1), Session()
    jobs = bare_jobs({"failed": job_row(failed), "healthy": job_row(healthy)})
    with pytest.raises(PolicyError, match="cleanup|close"):
        jobs.close()
    assert healthy.closed and jobs.rows["failed"]["status"] == "cleanup_failed"
    jobs.close()
    assert failed.closed


def test_closed_job_manager_never_launches_an_unmonitored_process(owner, tmp_path):
    launches = []

    def create(*args):
        launches.append(args)
        return Session()

    jobs = Jobs(owner, lambda: "fixture", factory=create)
    program = tmp_path / "fixture.exe"
    program.write_bytes(b"synthetic executable")
    jobs.close()
    try:
        with pytest.raises(PolicyError, match="closed"):
            jobs.start(str(program), [], str(tmp_path), 30, "fixture")
        assert launches == []
    finally:
        jobs.close()


def test_grant_observation_error_revokes_owned_jobs_instead_of_killing_the_monitor():
    session = Session()
    jobs = bare_jobs({"owned": job_row(session)})

    class OneTick:
        count = 0

        def wait(self, _):
            self.count += 1
            return self.count > 1

    def unavailable():
        raise OSError("Simulated authorization store failure")

    jobs.done = OneTick()
    jobs.grant_loader = unavailable
    jobs._watch()
    assert session.closed and jobs.rows["owned"]["status"] == "revoked"


@pytest.mark.parametrize(
    "state",
    ["timed_out", "timeout", "output_limit", "cleanup_failed", "revoked", "expired"],
)
def test_native_job_failures_are_never_reported_as_pass(state):
    result = protocol.result({"status": state, "exit_code": None})
    assert result["isError"] is True
    assert result["_meta"]["udb"]["status"] == "ERROR"


def test_stale_pass_header_cannot_override_a_failed_structured_job_report():
    from udb.proxy import normalized_output

    row = normalized_output(
        "computer_process",
        {
            "content": [{"type": "text", "text": "PASS | stale backend label"}],
            "structuredContent": {"status": "cleanup_failed"},
            "isError": False,
        },
    )
    assert row["isError"] is True
    assert row["content"][0]["text"].startswith("ERROR |")


def test_deep_untrusted_text_is_preserved_without_crashing_response_normalization():
    from udb.proxy import normalized_output

    data = "[" * 2000 + "0" + "]" * 2000
    row = normalized_output(
        "read_file",
        {
            "content": [
                {"type": "text", "text": "PASS | Read file"},
                {"type": "text", "text": data},
            ]
        },
    )
    assert row["content"][1]["text"] == data


@pytest.mark.parametrize("state", ["stopped", "completed", "running"])
def test_intentional_stop_and_normal_job_statuses_keep_their_meaning(state):
    result = protocol.result({"status": state})
    assert result["isError"] is False
    assert result["_meta"]["udb"]["status"] == (
        "RUNNING" if state == "running" else "PASS"
    )


def test_host_shutdown_attempts_all_sessions_and_jobs_after_a_close_failure(service):
    failed, healthy = Session(failures=1), Session()
    add_session(service, "a" * 32, failed)
    add_session(service, "b" * 32, healthy, "workspace")
    service.worker = SimpleNamespace(join=lambda **kwargs: None)
    service.monitor = SimpleNamespace(join=lambda **kwargs: None)
    computer_closed = []
    service.computer = SimpleNamespace(close=lambda: computer_closed.append(True))
    with pytest.raises(PolicyError, match="cleanup"):
        service.close()
    assert computer_closed == [True] and healthy.closed
    assert not failed.closed and "a" * 32 in service.sessions
    service.close()
    assert failed.closed and not service.sessions


def test_failed_handoff_cleanup_is_retained_until_maintenance_can_retry(
    service, monkeypatch
):
    import udb.host_service as module

    sid, session = "a" * 32, Session(visible=True)
    add_session(service, sid, session)
    flag = service.agent.state / "browser-handoff.json"
    host_client.atomic_json(flag, {"active": True})
    original = module.atomic_json
    failures = [1]

    def locked(path, value):
        if path == flag and failures[0]:
            failures[0] -= 1
            raise OSError("Simulated transient handoff file lock")
        return original(path, value)

    monkeypatch.setattr(module, "atomic_json", locked)
    with pytest.raises(OSError):
        service._close(sid)
    assert session.closed and sid in service.sessions
    service._maintain()
    assert not service.sessions and not browser_handoff(service.agent)


def test_failed_return_preserves_live_owner_handoff_and_emergency_pause(service):
    from udb.desktop_control import pause_local, paused

    session = Session(visible=True)

    def failed(_):
        raise PolicyError("Simulated unchanged owner window")

    session.switch_presentation = failed
    add_session(service, "a" * 32, session)
    host_client.atomic_json(
        service.agent.state / "browser-handoff.json", {"active": True}
    )
    pause_local(service.agent, True)
    with pytest.raises(PolicyError, match="owner window"):
        service.dispatch("browser_presentation", {"visible": False})
    assert browser_handoff(service.agent) and paused(service.agent)


def test_actual_service_snapshot_with_a_closing_session_remains_readable(service):
    add_session(service, "a" * 32, Session(visible=True))
    service.sessions["a" * 32]["closing"] = True
    HostService.publish(service)
    result = host_client.status(service.agent)
    assert result["worker_ready"] is True
    assert result["sessions"][0]["closing"] is True


def test_health_reports_owner_opened_tabs_without_changing_admission_limits(service):
    session = Session(visible=True)
    session.tabs = {str(i): str(i) for i in range(6)}
    add_session(service, "a" * 32, session)
    HostService.publish(service)
    result = host_client.status(service.agent)
    assert result["worker_ready"] is True
    assert result["sessions"][0]["tabs"] == 6


def test_computer_status_without_worker_does_not_enqueue_or_grant_access(owner):
    host_client.atomic_json(
        owner.state / "host/status.json", health(worker_ready=False)
    )
    result = host_client.command(owner, "computer", mode="status")
    assert result["enabled"] is False and result["worker_ready"] is False
    assert result["processes"] == {"jobs": [], "available": False}
    assert not list((owner.state / "host/inbox").iterdir())
    assert not (owner.state / "computer-access.dpapi").exists()


@pytest.mark.parametrize(
    "raw", [b"[" * 2000, b'{"resources":{"at":NaN}}', b"x" * (128 * 1024 + 1)]
)
def test_deep_or_oversized_health_recovers_to_an_unavailable_state(owner, raw):
    (owner.state / "host/status.json").write_bytes(raw)
    assert host_client.status(owner)["worker_ready"] is False


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "b" * 32},
        {"op": None},
        {"args": []},
        {"expires": 0},
        {"expires": float("inf")},
    ],
)
def test_invalid_command_envelopes_never_reach_dispatch(service, changes):
    path = service.folder / "inbox" / ("a" * 32 + ".json")
    payload = dict(id=path.stem, op="stop_all", args={}, expires=time.time() + 25)
    payload.update(changes)
    host_client.atomic_json(path, payload)
    calls = []
    service.dispatch = lambda *args: calls.append(args)
    assert service._process_command(path)
    reply = json.loads((service.folder / "replies" / path.name).read_text())
    assert reply["ok"] is False and calls == [] and service.current is None


@pytest.fixture
def browser_startup(monkeypatch):
    import io

    from udb import host_browser

    clock = SimpleNamespace(now=0, reads=0)

    def sleep(seconds):
        clock.now += seconds

    monkeypatch.setattr(
        host_browser, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep)
    )

    def read(sequence):
        rows = iter(sequence)
        last = sequence[-1]

        def open_file(mode):
            assert mode == "rb"
            clock.reads += 1
            value = next(rows, last)
            if isinstance(value, Exception):
                raise value
            return io.BytesIO(value)

        return host_browser._browser_endpoint(
            SimpleNamespace(open=open_file), SimpleNamespace(alive=lambda: True), 0.2
        )

    return read, clock


@pytest.mark.parametrize("pending", [PermissionError(), FileNotFoundError(), b"9222\n"])
def test_browser_startup_waits_for_readable_complete_endpoint(browser_startup, pending):
    read, clock = browser_startup
    assert read([pending, b"9222\n/devtools/browser/owned"]) == (
        "9222",
        "/devtools/browser/owned",
    )
    assert clock.reads == 2 and clock.now == 0.05


@pytest.mark.parametrize("pending", [PermissionError(), b"9222\n"])
def test_browser_endpoint_wait_is_bounded(browser_startup, pending):
    read, clock = browser_startup
    with pytest.raises(PolicyError, match="startup timed out"):
        read([pending])
    assert clock.now == pytest.approx(0.2)


@pytest.mark.parametrize(
    "invalid",
    [b"0\n/devtools/browser/owned", b"x" * 4097, b"\xff", b"9222\n/foreign"],
)
def test_browser_endpoint_still_rejects_invalid_records(browser_startup, invalid):
    read, clock = browser_startup
    with pytest.raises(PolicyError, match="Invalid private browser endpoint"):
        read([invalid])
    assert clock.reads == 1


def test_browser_endpoint_stops_waiting_when_owned_process_exits():
    from udb.host_browser import _browser_endpoint

    with pytest.raises(PolicyError, match="exited during startup"):
        _browser_endpoint(None, SimpleNamespace(alive=lambda: False), 1)
