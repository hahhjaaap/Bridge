"""Isolation regression for guest-only UI Automation and visible AI cursor."""

import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from udb import desktop_guest as guest


def test_uia_rejects_unknown_action_without_running_shell(monkeypatch):
    d = guest.GuestDesktop.__new__(guest.GuestDesktop)

    def forbidden(*a, **k):
        raise AssertionError("Subprocess unexpectedly invoked")

    monkeypatch.setattr(guest.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="Unsupported"):
        d.uia_demo("powershell", text="unsafe")


def test_uia_arguments_are_fixed_no_shell_and_text_base64(monkeypatch):
    d = guest.GuestDesktop.__new__(guest.GuestDesktop)
    original = 'Sample "quotes" & metacharacters; ê✓'
    seen = []

    def capture(cmd, **kw):
        seen.append((cmd, kw))
        return SimpleNamespace(returncode=0, stdout=b"UIA_SET_VALUE_OK", stderr=b"")

    monkeypatch.setattr(guest, "_system_executable", lambda _: sys.executable)
    monkeypatch.setattr(guest.subprocess, "run", capture)
    assert d.uia_demo("type", text=original) == "UIA_SET_VALUE_OK"
    cmd, kw = seen[0]
    assert isinstance(cmd, list) and "shell" not in kw
    assert kw["stdin"] == guest.subprocess.DEVNULL
    assert cmd[cmd.index("-File") + 1].endswith("uia.ps1")
    assert base64.b64decode(cmd[cmd.index("-TextB64") + 1]).decode("utf8") == original
    assert original not in " ".join(cmd)
    assert kw["timeout"] <= 25


def test_guest_only_winforms_targets_and_overlay_asset():
    folder = Path(guest.__file__).parent
    uia = (folder / "desktop_uia.ps1").read_text("utf8")
    overlay = (folder / "desktop_overlay.ps1").read_text("utf8")
    assert "UDB Sandbox Pad - guest only" in uia
    assert "InvokePattern" in uia and "ValuePattern" in uia
    assert "Demo-only Confirm button not found at cursor coordinates" in uia
    assert "Demo-only native text edit not found" in uia
    assert "UDB Sandbox Pad - guest only" in uia
    assert "SendMessageTimeoutW" in uia
    assert "UdbAiCursor" in overlay and "ShowWithoutActivation" in overlay
    assert "0x00000020" in overlay and "0x08000000" in overlay
    assert "SendInput" not in overlay and "SetCursorPos" not in overlay


def test_guest_identity_guard_preserved():
    import inspect

    source = inspect.getsource(guest.GuestDesktop.__init__)
    assert source.index("assert_guest(config)") < source.index("ctypes.WinDLL(")


def test_mcp_desktop_action_keeps_no_host_shell():
    from udb.core import PolicyError
    from udb.desktop_vm import validate_action

    for act in ("shell", "exec", "host_cursor", "host_click", "launch_program"):
        with pytest.raises(PolicyError):
            validate_action(act)


def test_pyinstaller_includes_trusted_scripts():
    spec = (Path(__file__).resolve().parents[2] / "tools/udb.spec").read_text("utf8")
    assert "collect_data_files" in spec and "*.ps1" in spec
    assert Path(guest.__file__).with_name("desktop_uia.ps1").is_file()
    assert Path(guest.__file__).with_name("desktop_overlay.ps1").is_file()
