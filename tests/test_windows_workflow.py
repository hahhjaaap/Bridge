"""Cooperative yielding, local pause, clipboard conflicts and app selection."""

from types import SimpleNamespace

import pytest

from udb.core import Agent, PolicyError
from udb.desktop_control import InputControl, pause_local, paused
from udb.windows_utilities import Utilities


class Observer:
    def __init__(self):
        self.value = dict(tick=10, position=(10, 20), idle_seconds=0.1, emergency=False)

    def sample(self):
        return self.value.copy()


def test_human_activity_yields_and_own_injected_input_does_not(tmp_path):
    observer = Observer()
    control = InputControl(Agent(tmp_path), observer)
    with pytest.raises(PolicyError, match="User is active"):
        control.check()
    observer.value["idle_seconds"] = 3
    control.check()
    observer.value.update(tick=11, idle_seconds=0)
    control.note_injected()
    control.check()
    observer.value.update(tick=12, position=(99, 30))
    with pytest.raises(PolicyError, match="yielded"):
        control.check()


def test_emergency_pause_is_latched_until_local_resume(tmp_path):
    agent, observer = Agent(tmp_path), Observer()
    observer.value.update(emergency=True, idle_seconds=10)
    control = InputControl(agent, observer)
    with pytest.raises(PolicyError, match="paused locally"):
        control.check()
    observer.value["emergency"] = False
    assert paused(agent)
    assert control.status()["state"] == "paused"
    pause_local(agent, False)
    control.check()


def test_malformed_pause_record_fails_closed(tmp_path):
    agent = Agent(tmp_path)
    (agent.state / "desktop-paused.json").write_text("[]")
    assert paused(agent)


class Backend:
    def __init__(self):
        self.calls = []
        self.version = "7"
        self.text = "owner text"

    def clipboard(self, text=None, expected_version=""):
        if text is None:
            return dict(text=self.text, version=self.version)
        if expected_version != self.version:
            raise PolicyError("Clipboard changed")
        self.text = text
        self.calls.append(("clipboard", text))
        return {"written": True}

    def apps(self):
        return [dict(name="Editor", app_id="one"), dict(name="Editor", app_id="two")]

    def launch(self, app_id):
        self.calls.append(("launch", app_id))
        return {"requested": True}

    def notify(self, title, text):
        self.calls.append(("notify", title, text))
        return {"requested": True}


def utilities(tmp_path):
    backend = Backend()
    return Utilities(
        Agent(tmp_path), backend, SimpleNamespace(check=lambda: None)
    ), backend


def test_clipboard_requires_matching_version_and_never_replays(tmp_path):
    tool, backend = utilities(tmp_path)
    first = tool.dispatch("clipboard_get")
    backend.version = "8"
    with pytest.raises(PolicyError, match="Clipboard changed"):
        tool.dispatch("clipboard_set", text="new", expected_version=first["version"])
    assert backend.text == "owner text" and backend.calls == []
    tool.dispatch("clipboard_set", text="new", expected_version="8")
    assert backend.calls == [("clipboard", "new")]


def test_ambiguous_app_names_require_exact_app_id(tmp_path):
    tool, backend = utilities(tmp_path)
    with pytest.raises(PolicyError, match="ambiguous"):
        tool.dispatch("launch_app", name="Editor")
    assert backend.calls == []
    tool.dispatch("launch_app", name="two")
    assert backend.calls == [("launch", "two")]


def test_local_pause_blocks_app_launch_but_allows_read_and_notification(tmp_path):
    tool, backend = utilities(tmp_path)

    def stop():
        raise PolicyError("paused locally")

    tool.control = SimpleNamespace(check=stop)
    with pytest.raises(PolicyError, match="paused"):
        tool.dispatch("launch_app", name="one")
    assert tool.dispatch("clipboard_get")["text"] == "owner text"
    tool.dispatch("notify", title="UDB", text="Done")
    assert backend.calls == [("notify", "UDB", "Done")]


def test_utilities_require_owner_grant_before_backend_creation(tmp_path, monkeypatch):
    from udb.computer import Computer

    computer = Computer.__new__(Computer)
    computer.agent = Agent(tmp_path)
    monkeypatch.setattr(
        "udb.computer.access.require",
        lambda agent: (_ for _ in ()).throw(PolicyError("no grant")),
    )
    with pytest.raises(PolicyError, match="no grant"):
        computer.dispatch("utilities", {"action": "clipboard_get"}, {})


def test_paused_desktop_does_not_resolve_or_focus_a_window(tmp_path):
    from udb.computer_desktop import Desktop

    desktop = Desktop.__new__(Desktop)
    observer = Observer()
    desktop.control = InputControl(Agent(tmp_path), observer)
    desktop.resolve = lambda *args, **kwargs: pytest.fail("must yield before focusing")
    with pytest.raises(PolicyError, match="User is active"):
        desktop._dispatch("click", window_id="window", x=10, y=10)
    assert desktop._dispatch("control_status")["state"] == "user_active"


def test_native_clipboard_conflict_closes_lock_without_clearing(tmp_path, monkeypatch):
    import sys

    from udb.windows_utilities import WindowsBackend

    calls = []
    clip = SimpleNamespace(
        OpenClipboard=lambda: calls.append("open"),
        CloseClipboard=lambda: calls.append("close"),
        GetClipboardSequenceNumber=lambda: 8,
        EmptyClipboard=lambda: calls.append("clear"),
    )
    monkeypatch.setitem(sys.modules, "win32clipboard", clip)
    monkeypatch.setitem(sys.modules, "win32con", SimpleNamespace(CF_UNICODETEXT=13))
    with pytest.raises(PolicyError, match="Clipboard changed"):
        WindowsBackend().clipboard("new", "7")
    assert calls == ["open", "close"]


def test_notification_text_is_data_and_never_a_powershell_command(monkeypatch):
    import json

    from udb.windows_utilities import WindowsBackend

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"ok":true,"requested":true}')

    monkeypatch.setattr("udb.windows_utilities.subprocess.run", run)
    monkeypatch.setattr(
        "udb.windows_utilities.system_executable", lambda name: "powershell.exe"
    )
    text = "$(Start-Process bad); <text>Xin chào</text>"
    assert WindowsBackend().notify("UDB", text)["requested"]
    argv, options = calls[0]
    assert options["shell"] is False and "-File" in argv
    assert all(text not in argument for argument in argv)
    assert json.loads(options["input"])["text"] == text


def test_human_browser_handoff_blocks_desktop_without_clearing_emergency_pause(
    tmp_path,
):
    from udb.host_client import atomic_json

    agent, observer = Agent(tmp_path), Observer()
    observer.value["idle_seconds"] = 10
    control = InputControl(agent, observer)
    atomic_json(agent.state / "browser-handoff.json", {"active": True})
    with pytest.raises(PolicyError, match="owner has browser control"):
        control.check()
    pause_local(agent)
    atomic_json(agent.state / "browser-handoff.json", {"active": False})
    with pytest.raises(PolicyError, match="paused locally"):
        control.check()


def test_control_status_remains_readable_without_resource_admission(
    tmp_path, monkeypatch
):
    import asyncio

    from udb.server import make_server

    calls = []
    monkeypatch.setattr("udb.computer_access.require", lambda agent: "grant")
    monkeypatch.setattr(
        "udb.desktop_control.InputControl",
        lambda agent: SimpleNamespace(
            status=lambda: calls.append("status") or {"state": "paused"}
        ),
    )
    monkeypatch.setattr(
        "udb.host_client.status",
        lambda *a: pytest.fail("status must not require resource admission"),
    )
    server = make_server(Agent(tmp_path))
    asyncio.run(server.call_tool("computer_desktop", {"action": "control_status"}))
    assert calls == ["status"]
