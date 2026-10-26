"""Headless lifecycle: no permission mutations, replay or GUI dependency."""

import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from udb import headless
from udb import owner_command as cli
from udb.core import PolicyError


class Controller:
    def __init__(self, state):
        self.agent = SimpleNamespace(state=state)
        self.closed = threading.Event()
        self.host_calls = 0
        self.connect_calls = 0
        self.close_calls = 0

    def start_host(self):
        self.host_calls += 1

    def start_tunnel(self):
        self.connect_calls += 1
        raise PolicyError("synthetic connection unavailable")

    def status(self):
        return dict(ready=False, tunnel_running=False, connecting=False)

    def close(self):
        self.close_calls += 1
        self.closed.set()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"instance": "wrong", "expires": 110},
        {"instance": "a" * 32, "expires": 99},
        {"instance": "a" * 32, "expires": 161},
        {"instance": "a" * 32, "expires": True},
        {"instance": "a" * 32, "expires": "110"},
        {"instance": "a" * 32, "expires": float("nan")},
    ],
)
def test_stop_ignores_malformed_expired_foreign_or_unbounded_requests(
    tmp_path, payload
):
    path = tmp_path / "stop.json"
    path.write_text(json.dumps(payload))
    assert not headless.stop_requested(path, "a" * 32, now=100)


def test_local_stop_matches_exact_process_lifetime(tmp_path):
    path = tmp_path / "stop.json"
    path.write_text(json.dumps(dict(instance="a" * 32, expires=110)))
    assert headless.stop_requested(path, "a" * 32, now=100)
    assert not headless.stop_requested(path, "b" * 32, now=100)
    path.write_bytes(b"x" * 4097)
    assert not headless.stop_requested(path, "a" * 32, now=100)


def test_offline_lifecycle_has_worker_without_connect_or_grant_changes(tmp_path):
    ctl = Controller(tmp_path)
    settings = tmp_path / "host-settings.json"
    settings.write_text('{"enabled":false}')
    (tmp_path / headless.STOP_NAME).write_text(
        json.dumps(dict(instance="a" * 32, expires=time.time() + 20))
    )
    headless.run_loop(ctl, "a" * 32, threading.Event(), connect=False, interval=0.001)
    result = json.loads((tmp_path / headless.STATUS_NAME).read_text())
    assert ctl.host_calls == ctl.close_calls == 1 and ctl.connect_calls == 0
    assert result["phase"] == "stopped" and result["mode"] == "headless"
    assert result["unrestricted_host_access"] is False
    assert settings.read_text() == '{"enabled":false}'


def test_startup_failure_still_closes_owned_controller(tmp_path):
    ctl = Controller(tmp_path)

    def fail():
        raise RuntimeError("synthetic startup failure")

    ctl.start_host = fail
    with pytest.raises(RuntimeError, match="synthetic"):
        headless.run_loop(ctl, "a" * 32, threading.Event(), connect=False)
    assert ctl.close_calls == 1


def test_headless_heartbeat_is_bounded_and_never_claims_full_access(tmp_path):
    ctl = Controller(tmp_path)
    headless.publish(ctl, "a" * 32)
    assert headless.manager_status(tmp_path) == dict(
        mode="headless", gui_loaded=False, unrestricted_host_access=False
    )
    data = json.loads((tmp_path / headless.STATUS_NAME).read_text())
    data["at"] = 0
    (tmp_path / headless.STATUS_NAME).write_text(json.dumps(data))
    assert headless.manager_status(tmp_path)["mode"] == "unknown_or_stopped"


def test_cli_stop_writes_scoped_request_and_waits_for_exit(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    original = dict(instance="a" * 32)
    reports = iter([dict(manager_running=True), dict(manager_running=False)])
    with patch.object(cli, "headless_state", return_value=original):
        result = cli.stop(
            tmp_path, tmp_path / "synthetic.exe", snapshot=lambda *a: next(reports)
        )
    request = json.loads((state / headless.STOP_NAME).read_text())
    assert request["instance"] == original["instance"]
    assert time.time() < request["expires"] <= time.time() + 45
    assert result["manager_running"] is False


def test_cli_cannot_stop_legacy_or_unidentified_process(tmp_path):
    with patch.object(cli, "headless_state", return_value=None):
        with pytest.raises(cli.CommandError, match="Only the headless"):
            cli.stop(
                tmp_path,
                tmp_path / "synthetic.exe",
                snapshot=lambda *a: dict(manager_running=True),
            )
    assert not (tmp_path / "state" / headless.STOP_NAME).exists()


def test_headless_does_not_import_qtwidgets_in_fresh_interpreter(tmp_path):
    import os
    import subprocess

    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; import udb.headless; assert 'PySide6.QtWidgets' not in sys.modules"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
