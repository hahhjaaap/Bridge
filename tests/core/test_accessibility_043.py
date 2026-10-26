"""Guest accessibility API safety and contract regression tests (no live VM)."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import desktop_guest
from udb.core import Agent, PolicyError
from udb.desktop_vm import validate_action
from udb.server import make_server


def test_new_tools_are_registered(tmp_path):
    server = make_server(Agent(tmp_path))
    tools = {item.name for item in server._tool_manager.list_tools()}
    assert {"desktop_snapshot", "desktop_wait_for", "desktop_element_action"} <= tools


def test_uia_guest_scoping_and_limits():
    assert validate_action("snapshot")["action"] == "snapshot"
    assert (
        validate_action("wait_for", name="Save", timeout_seconds=12)["name"] == "Save"
    )
    assert (
        validate_action("element_invoke", automation_id="btnSave")["automation_id"]
        == "btnSave"
    )
    assert (
        validate_action("element_set_text", name="Document", value="hello")["value"]
        == "hello"
    )
    assert (
        validate_action("element_set_text", control_type="Edit", value="hello")[
            "control_type"
        ]
        == "Edit"
    )
    for action in ("wait_for", "element_invoke", "element_set_text"):
        with pytest.raises(PolicyError, match="requires"):
            validate_action(action)
    with pytest.raises(PolicyError):
        validate_action("element_set_text", name="X", value="a" * 2001)
    with pytest.raises(PolicyError):
        validate_action("wait_for", name="X", timeout_seconds=40)
    with pytest.raises(PolicyError):
        validate_action("host_click")


def test_guest_uia_uses_one_persistent_guest_script(monkeypatch):
    desktop = desktop_guest.GuestDesktop.__new__(desktop_guest.GuestDesktop)
    seen = []

    class FakeIn:
        def __init__(self):
            self.lines = []

        def write(self, value):
            self.lines.append(value)

        def flush(self):
            pass

        def close(self):
            pass

    class FakeProcess:
        def __init__(self):
            self.stdin = FakeIn()
            self.stdout = __import__("io").StringIO(
                'READY\nRES:{"ok":true,"found":true}\nRES:{"ok":true,"action":"invoke"}\n'
            )
            self.stderr = None

        def poll(self):
            return None

        def wait(self, timeout=None):
            return 0

    def fake_popen(argv, **kwargs):
        seen.append((argv, kwargs))
        return FakeProcess()

    monkeypatch.setattr(desktop_guest.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        desktop_guest,
        "_system_executable",
        lambda name: str(Path.cwd() / "fake-system" / name),
    )
    assert desktop.accessibility("wait_for", name="Save") == {"ok": True, "found": True}
    assert desktop.accessibility("element_invoke", name="Save") == {
        "ok": True,
        "action": "invoke",
    }
    assert len(seen) == 1
    argv, kwargs = seen[0]
    assert Path(argv[0]).name.lower() == "powershell.exe"
    assert Path(argv[0]).is_absolute()
    assert "accessibility.ps1" in argv[argv.index("-File") + 1]
    assert "-Persistent" in argv
    assert kwargs["stdin"] == desktop_guest.subprocess.PIPE
    assert "shell" not in kwargs
    requests = [
        json.loads(base64.b64decode(line.strip()).decode("utf8"))
        for line in desktop._uia_process.stdin.lines
    ]
    assert [req["mode"] for req in requests] == ["wait_for", "element_invoke"]
    assert requests[0]["name"] == "Save"


def test_uia_helper_has_guest_guard_and_no_host_fallback():
    script = (
        Path(desktop_guest.__file__).parent / "desktop_accessibility.ps1"
    ).read_text("utf8")
    assert "WDAGUtilityAccount" in script
    assert "GetForegroundWindow" in script
    assert "InvokePattern" in script
    assert "ValuePattern" in script
    assert "Start-Process" not in script
    assert "SetCursorPos" not in script
    assert "SendInput" not in script
