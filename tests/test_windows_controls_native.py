"""Native UIA patterns on one Job-owned disposable WPF form; no owner grant."""

import json
import os
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows UIA fixture")


def test_native_control_patterns_on_one_owned_window(tmp_path, monkeypatch):
    import subprocess

    import win32gui
    import win32process

    from udb.computer_desktop import Desktop
    from udb.core import PolicyError
    from udb.owned_process import OwnedProcess
    from udb.windows_paths import system_executable

    ready = tmp_path / "ready.json"
    script = Path(__file__).parent / "fixtures/windows_controls.ps1"
    process = OwnedProcess(
        [
            system_executable("powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Sta",
            "-File",
            str(script),
            "-ReadyPath",
            str(ready),
        ],
        tmp_path,
    )
    run = subprocess.run
    last_helper = []

    def trace(*args, **kwargs):
        result = run(*args, **kwargs)
        if args and str(args[0][-1]).endswith("computer_uia.ps1"):
            last_helper[:] = [result.stdout, result.stderr]
        return result

    monkeypatch.setattr(subprocess, "run", trace)
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            error = ready.with_name(ready.name + ".error")
            assert process.alive(), (
                error.read_text()
                if error.exists()
                else "Owned fixture exited before readiness"
            )
            time.sleep(0.05)
        data = json.loads(ready.read_text())
        hwnd = data["hwnd"]
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        assert pid == data["pid"] and process.owns_pid(pid)
        desktop = Desktop(guard=lambda: process.alive())
        wid = uuid.uuid4().hex

        def action(mode, **args):
            assert process.owns_pid(win32process.GetWindowThreadProcessId(hwnd)[1])
            desktop.windows[wid] = (
                hwnd,
                pid,
                win32gui.GetClassName(hwnd),
                time.monotonic() + 60,
            )
            try:
                return desktop.dispatch(mode, window_id=wid, **args)
            except PolicyError as exc:
                exc.add_note("Owned fixture helper output: " + repr(last_helper))
                raise

        cursor, foreground = win32gui.GetCursorPos(), win32gui.GetForegroundWindow()
        tree = action("snapshot")["elements"]
        assert any(
            e.get("automation_id") == "Choice" and "Toggle" in e.get("patterns", [])
            for e in tree
        )
        assert action(
            "read_text",
            automation_id="Editor",
            control_type="ControlType.Edit",
            limit=3,
        )["truncated"]
        assert action("read_text", automation_id="Editor", limit=1)["text"] == ""
        assert (
            action("read_text", automation_id="Locked")["text"].rstrip("\r\n")
            == "readonly fixture"
        )
        with pytest.raises(PolicyError):
            action("read_text", automation_id="Secret")
        assert win32gui.GetForegroundWindow() == foreground
        first = action("set_checked", automation_id="Choice", checked=True)
        again = action("set_checked", automation_id="Choice", checked=True)
        assert first["checked"] and first["changed"] and not again["changed"]
        assert action("read_text", automation_id="Counter")["text"].strip() == "1"
        assert action("select", automation_id="Items", text="Second item")["selected"]
        assert action("expand", automation_id="Country")["expanded"]
        assert action("select", automation_id="Country", text="Vietnam")["selected"]
        assert action("collapse", automation_id="Country")["expanded"] is False
        assert action("expand", automation_id="Details")["expanded"]
        assert action("collapse", automation_id="Details")["expanded"] is False
        assert action("scroll_into_view", automation_id="Row19")["offscreen"] is False
        assert win32gui.GetCursorPos() == cursor
        closed = action("close_window", wait_ms=1000)
        assert (
            closed["requested"]
            and closed["forced"] is False
            and closed["closed_observed"]
        )
    finally:
        process.close()
