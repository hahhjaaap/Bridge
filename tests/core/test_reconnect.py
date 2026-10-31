"""Reconnect timing and cancellation, without credentials or real tunnel access."""

import subprocess
from types import SimpleNamespace

import pytest

from udb import desktop
from udb.core import PolicyError
from udb.desktop import DesktopController


@pytest.fixture
def controller(tmp_path):
    ctl = DesktopController(tmp_path / "bridge", monitor=False)
    yield ctl
    ctl.close()


def test_automation_queue_failure_does_not_expose_private_exception(controller, monkeypatch):
    from unittest.mock import Mock

    secret = "PRIVATE_JOB_PAYLOAD"
    controller.closed = Mock()
    controller.closed.wait.side_effect = [False, True]

    def fail():
        raise RuntimeError(secret)

    monkeypatch.setattr(controller.agent, "pump_sandbox_queue_local", fail)
    controller._sandbox_monitor()
    assert secret not in controller.error
    assert "Automation queue failed" in controller.error


def test_no_connection_without_user_intent(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller, "_launch_tunnel", lambda generation: calls.append(generation)
    )
    controller._retry_if_due(100)
    assert not calls


def test_crash_retries_with_bounded_backoff_and_stop_cancels(controller, monkeypatch):
    clock = [100.0]
    calls = []
    monkeypatch.setattr(desktop.time, "monotonic", lambda: clock[0])

    def launch(generation):
        calls.append(generation)
        raise RuntimeError("synthetic-secret-do-not-display")

    monkeypatch.setattr(controller, "_launch_tunnel", launch)
    with pytest.raises(PolicyError) as error:
        controller.start_tunnel()
    assert "synthetic-secret" not in str(error.value)
    assert "synthetic-secret" not in str(controller.status())
    assert controller._retry_at == 101
    controller._retry_if_due(100.9)
    assert len(calls) == 1
    for expected_delay in (2, 4, 8, 16, 30, 30):
        clock[0] = controller._retry_at
        controller._retry_if_due()
        assert controller._retry_at - clock[0] == expected_delay
    count = len(calls)
    controller.stop_process("tunnel")  # Also works when the child already exited.
    controller._retry_if_due(10**6)
    assert len(calls) == count
    assert not controller.status()["tunnel_wanted"]


def test_disconnect_during_init_prevents_late_launch(controller, monkeypatch):
    (controller.home / "tools").mkdir()
    (controller.home / "tools" / "tunnel-client.exe").write_bytes(b"synthetic")
    monkeypatch.setattr(
        controller, "config", lambda: {"tunnel_id": "tunnel_" + "a" * 32}
    )
    monkeypatch.setattr(desktop, "load_key", lambda: "synthetic-key")

    def initialize(*args, **kwargs):
        controller.stop_process("tunnel")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", initialize)

    def forbidden(*args, **kwargs):
        pytest.fail("A canceled connection launched a child")

    monkeypatch.setattr(controller, "start_process", forbidden)
    controller.start_tunnel()
    assert not controller.tunnel_wanted and not controller._connecting


def test_close_prevents_any_future_retry(controller, monkeypatch):
    controller.tunnel_wanted = True
    controller.close()
    monkeypatch.setattr(
        controller, "_launch_tunnel", lambda generation: pytest.fail("closed")
    )
    controller._retry_if_due(10**6)
    with pytest.raises(PolicyError):
        controller.start_tunnel()


def test_repeated_child_restarts_close_old_log_handles(controller, monkeypatch):
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **kw: SimpleNamespace(poll=lambda: 0)
    )
    controller.start_process("tunnel", ["synthetic"])
    previous = controller.handles["tunnel"]
    controller.start_process("tunnel", ["synthetic"])
    assert previous.closed and len(controller.handles) == 1


def test_concurrent_connect_does_not_spawn_twice(controller, monkeypatch):
    calls = []

    def launch(generation):
        calls.append(generation)
        controller.start_tunnel()

    monkeypatch.setattr(controller, "_launch_tunnel", launch)
    controller.start_tunnel()
    assert len(calls) == 1


def test_disconnect_between_retry_check_and_launch_is_not_reenabled(
    controller, monkeypatch
):
    controller.tunnel_wanted = True
    original = controller.start_tunnel

    def race(**kwargs):
        controller.stop_process("tunnel")
        original(**kwargs)

    monkeypatch.setattr(controller, "start_tunnel", race)
    monkeypatch.setattr(
        controller,
        "_launch_tunnel",
        lambda generation: pytest.fail("retry raced past Stop"),
    )
    controller._retry_if_due(10**6)
    assert not controller.tunnel_wanted


def test_status_checks_sandbox_without_full_diagnostics(controller, monkeypatch):
    from udb import sandbox

    monkeypatch.setattr(
        controller.agent,
        "diagnostics",
        lambda: pytest.fail("status must not rebuild the full diagnostics report"),
    )
    monkeypatch.setattr(sandbox, "available", lambda: True)
    assert controller.status()["sandbox"] is True
