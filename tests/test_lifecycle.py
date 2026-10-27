"""Recovery, diagnostics and local lifecycle tests without real credentials."""

import base64
import contextlib
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import autostart, cli, maintenance, onboarding, owner_command
from udb.config import Settings
from udb.desktop import DesktopController
from udb.owner_command import CommandError


@pytest.fixture
def controller(tmp_path, monkeypatch):
    ctl = DesktopController(tmp_path / "bridge", monitor=False)
    child = SimpleNamespace(live=True)
    child.poll = lambda: None if child.live else 0
    ctl.children["tunnel"] = child
    ctl.tunnel_wanted = True
    monkeypatch.setattr(owner_command, "tunnel_ready", lambda *a: False)
    original_stop = ctl.stop_process

    def stop(name):
        child.live = False
        original_stop(name)

    monkeypatch.setattr(ctl, "stop_process", stop)
    yield ctl
    ctl.close()


def test_live_unready_tunnel_recovers_once_after_grace(controller):
    for now in range(0, 91, 10):
        controller._monitor_once(None, now)
    assert not controller.alive("tunnel")
    assert controller.tunnel_wanted and controller._recovery_count == 1
    assert controller._retry_at > 0
    assert controller._last_recovery == "tunnel_unready"


def test_short_outage_and_sleep_grace_do_not_restart(controller, monkeypatch):
    controller._monitor_once(None, 0)
    controller._monitor_once(None, 15)
    controller._monitor_once(None, 4000)  # Resume gets a new full grace period.
    controller._monitor_once(None, 4010)
    assert controller.alive("tunnel") and controller._recovery_count == 0
    monkeypatch.setattr(owner_command, "tunnel_ready", lambda *a: True)
    controller._monitor_once(None, 4011)
    assert controller.ready and controller._unready_since is None


def test_late_ready_response_cannot_mark_a_replaced_child_ready(
    controller, monkeypatch
):
    def response(*a):
        controller.children["tunnel"] = SimpleNamespace(poll=lambda: None)
        return True

    monkeypatch.setattr(owner_command, "tunnel_ready", response)
    controller._monitor_once(None, 0)
    assert not controller.ready
    controller.children["tunnel"].poll = lambda: 0


def test_stop_during_health_check_cancels_recovery(controller, monkeypatch):
    def response(*a):
        controller.stop_process("tunnel")
        return False

    monkeypatch.setattr(owner_command, "tunnel_ready", response)
    controller._monitor_once(None, 1000)
    assert not controller.tunnel_wanted and not controller._recovery_count


class Registry:
    HKEY_CURRENT_USER = 1
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {}

    def OpenKey(self, *a):
        return contextlib.nullcontext(self)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError()
        return self.values[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = (value, kind)

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError()
        del self.values[name]


def test_autostart_is_optional_idempotent_and_never_grants(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "home", tmp_path / "work")
    registry = Registry()
    monkeypatch.setattr(autostart, "startup_command", lambda _: "safe-test-command")
    assert not autostart.status(settings, registry)["enabled"]
    with pytest.raises(CommandError, match="setup"):
        autostart.run(settings, "enable", backend=registry)
    assert not settings.home.exists()
    onboarding.save_mode(settings, "offline")
    registry.values["Unrelated"] = ("leave alone", registry.REG_SZ)
    for _ in range(2):
        assert autostart.run(settings, "enable", backend=registry) == 0
        assert autostart.status(settings, registry)["matches_current"]
    assert not (settings.home / "state/computer-access.dpapi").exists()
    for _ in range(2):
        autostart.run(settings, "disable", backend=registry)
    assert registry.values == {"Unrelated": ("leave alone", registry.REG_SZ)}


def test_startup_arguments_are_literals_and_background_only(tmp_path, monkeypatch):
    settings = Settings(tmp_path / "owner's $home; echo injected", tmp_path / "work")
    monkeypatch.setattr(autostart, "installation", lambda: None)
    monkeypatch.setattr(
        autostart, "system_executable", lambda _: r"C:\Windows\powershell.exe"
    )
    value = autostart.startup_command(settings)
    script = base64.b64decode(value.split()[-1]).decode("utf-16-le")
    assert "owner''s $home; echo injected" in script
    assert "'--detach'" in script and "'--access'" not in script
    assert "-WindowStyle Hidden" in value


@pytest.mark.skipif(os.name != "nt", reason="Windows registry")
def test_registry_round_trip_in_isolated_nonstartup_key(tmp_path, monkeypatch):
    import uuid
    import winreg

    key = "Software\\UDB-Test-" + uuid.uuid4().hex
    monkeypatch.setattr(autostart, "RUN_KEY", key)
    settings = Settings(tmp_path / "home", tmp_path / "work")
    onboarding.save_mode(settings, "offline")
    try:
        autostart.run(settings, "enable")
        assert autostart.status(settings)["matches_current"]
        autostart.run(settings, "disable")
        assert not autostart.status(settings)["enabled"]
    finally:
        with contextlib.suppress(FileNotFoundError):
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


def managed(tmp_path):
    root = tmp_path / "install"
    digest = "a" * 64
    exe = root / "releases" / digest[:24] / "udb.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"synthetic-not-an-executable")
    record = dict(
        product="UniversalDesktopBridge",
        schema=1,
        version="0.10.0",
        exe=str(exe),
        home=str(tmp_path / "home"),
        archive_sha256=digest,
    )
    (root / "install.json").write_text(json.dumps(record))
    return root, record, exe


def test_installation_only_matches_its_active_image(tmp_path):
    root, record, exe = managed(tmp_path)
    assert maintenance.installation(exe) == (root, record)
    assert maintenance.installation(tmp_path / "portable/udb.exe") is None
    record["exe"] = str(tmp_path / "unrelated.exe")
    (root / "install.json").write_text(json.dumps(record))
    with pytest.raises(CommandError):
        maintenance.installation(exe)


@pytest.mark.parametrize("tag", ["v0.10.1", "v1.0.0"])
def test_update_check_only_reads_public_metadata(tmp_path, monkeypatch, capsys, tag):
    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self, size):
            return json.dumps({"tag_name": tag}).encode()

    def open_url(req, timeout):
        assert (
            req.full_url
            == f"https://api.github.com/repos/{maintenance.REPOSITORY}/releases/latest"
        )
        assert not req.has_header("Authorization")
        return Reply()

    monkeypatch.setattr(maintenance.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(maintenance, "installation", lambda: None)
    monkeypatch.setattr(
        maintenance.subprocess,
        "run",
        lambda *a, **k: pytest.fail("check must not mutate"),
    )
    assert (
        maintenance.run(
            "update", Settings(tmp_path, tmp_path), check=True, as_json=True
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["latest"] == tag[1:]


def test_update_error_does_not_echo_server_or_child_output(
    tmp_path, monkeypatch, capsys
):
    def fail(*a, **k):
        raise OSError("SYNTHETIC_PRIVATE")

    monkeypatch.setattr(maintenance.urllib.request, "urlopen", fail)
    with pytest.raises(CommandError) as error:
        maintenance.latest_release()
    assert "SYNTHETIC_PRIVATE" not in str(error.value)


def test_rollback_delegates_fixed_installer_and_preserves_home(
    tmp_path, monkeypatch, capsys
):
    root, record, exe = managed(tmp_path)
    monkeypatch.setattr(maintenance, "installation", lambda: (root, record))
    monkeypatch.setattr(
        maintenance, "installer_script", lambda: tmp_path / "install.ps1"
    )
    from udb import windows_paths

    monkeypatch.setattr(
        windows_paths, "system_executable", lambda _: "absolute-powershell.exe"
    )

    def run(args, **kwargs):
        assert "-Rollback" in args and "-NoPath" in args and "-NoStart" in args
        assert record["home"] in args and kwargs["shell"] is False
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(maintenance.subprocess, "run", run)
    assert (
        maintenance.run(
            "rollback", Settings(Path(record["home"]), tmp_path), as_json=True
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["home_preserved"]


def test_doctor_offline_is_useful_without_credentials(tmp_path, monkeypatch):
    settings = Settings(tmp_path, tmp_path / "work")
    onboarding.save_mode(settings, "offline")
    monkeypatch.setattr(owner_command, "headless_state", lambda _: {})
    from udb import credentials

    monkeypatch.setattr(
        credentials, "load_key", lambda: pytest.fail("doctor must never read keys")
    )
    report = dict(
        manager_running=True,
        local_tunnel_ready=False,
        connection_mode="offline",
        next_action="local",
    )
    checks = owner_command.doctor(tmp_path, report)
    assert all(c["ok"] is not False for c in checks)
    assert next(c for c in checks if c["check"] == "credentials")["ok"] is None


def test_online_start_overrides_saved_local_mode(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(owner_command, "main", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(onboarding, "attended_console", lambda: False)
    onboarding.save_mode(Settings(tmp_path, tmp_path / "work"), "offline")
    assert cli.main(["start", "--home", str(tmp_path), "--online", "--json"]) == 0
    assert "--offline" not in calls[0]


def test_unready_stop_failure_keeps_reconnect_intent(controller, monkeypatch):
    def fail(name):
        controller.tunnel_wanted = False
        raise subprocess.TimeoutExpired("synthetic-stop", 15)

    monkeypatch.setattr(controller, "stop_process", fail)
    controller._recover_unready(controller._generation)
    assert controller.tunnel_wanted and not controller._recovery_count
    assert controller.alive("tunnel")
    monkeypatch.setattr(controller, "stop_process", lambda name: None)


def test_diagnostic_recovery_fields_are_bounded_not_raw_state(tmp_path, monkeypatch):
    monkeypatch.setattr(
        owner_command,
        "headless_state",
        lambda _: {
            "recovery_count": "SYNTHETIC_PRIVATE",
            "last_recovery": "SYNTHETIC_PRIVATE",
        },
    )
    report = owner_command.inspect(
        tmp_path, Path("unused"), running=lambda *a: True, ready=lambda *a: False
    )
    assert report["tunnel_recovery"] == {
        "count": 0,
        "last_reason": "",
        "replays_tool_calls": False,
    }
    assert "SYNTHETIC_PRIVATE" not in json.dumps(report)


def test_setup_checks_frozen_runtime_before_asking_for_a_key(tmp_path, monkeypatch):
    from udb import components

    monkeypatch.setattr(onboarding, "console_input", lambda: True)
    monkeypatch.setattr(onboarding.sys, "frozen", True, raising=False)
    monkeypatch.setattr(
        components, "runtime_report", lambda: {"components": [], "office_modules": {}}
    )
    monkeypatch.setattr(
        "builtins.input",
        lambda _: pytest.fail("Incomplete build must not request connection details"),
    )
    with pytest.raises(ValueError, match="Runtime check failed"):
        onboarding.run(Settings(tmp_path / "unused", tmp_path / "work"))
    assert not (tmp_path / "unused").exists()


def test_timed_out_update_reports_unknown_outcome_without_retry(tmp_path, monkeypatch):
    root, record, exe = managed(tmp_path)
    monkeypatch.setattr(maintenance, "installation", lambda: (root, record))
    monkeypatch.setattr(
        maintenance, "installer_script", lambda: tmp_path / "install.ps1"
    )
    from udb import windows_paths

    monkeypatch.setattr(
        windows_paths, "system_executable", lambda _: "absolute-powershell.exe"
    )
    calls = []

    def timeout(args, **kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired("synthetic-installer", 900)

    monkeypatch.setattr(maintenance.subprocess, "run", timeout)
    with pytest.raises(CommandError, match="outcome is unknown"):
        maintenance.run("rollback", Settings(Path(record["home"]), tmp_path))
    assert len(calls) == 1
