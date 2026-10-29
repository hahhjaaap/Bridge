"""Real overlay visibility on one Job-owned synthetic tab strip."""

import json
import os
import queue
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Native Windows overlay")


def test_native_overlay_follows_registered_tab_without_input(tmp_path, monkeypatch):
    import win32gui
    import win32process

    from udb import chrome_tab_identity as tabs
    from udb.companion.virtual_pointer import VirtualPointer
    from udb.computer_desktop import Desktop
    from udb.owned_process import OwnedProcess
    from udb.windows_paths import system_executable

    # The real owner's marker file and applications are never touched.
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    ready = tmp_path / "ready.json"
    process = OwnedProcess(
        [
            system_executable("powershell.exe"),
            "-NoProfile",
            "-NonInteractive",
            "-Sta",
            "-File",
            str(Path(__file__).parent / "fixtures/native_tab_identity.ps1"),
            "-ReadyPath",
            str(ready),
        ],
        tmp_path,
    )
    pointer = None
    try:
        deadline = time.monotonic() + 20
        while not ready.exists() and time.monotonic() < deadline:
            error = ready.with_name(ready.name + ".error")
            assert process.alive(), (
                error.read_text() if error.exists() else "Fixture exited"
            )
            time.sleep(0.05)
        hwnd = json.loads(ready.read_text())["hwnd"]
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        assert process.owns_pid(pid)
        win32gui.SetForegroundWindow(hwnd)
        before = tabs.observe_tabs(hwnd)
        assert before and len(before) == 2
        selected = [row for row in before if row["selected"]]
        assert len(selected) == 1
        previous = [row for row in before if not row["selected"]]
        assert tabs.register_new_tab(hwnd, pid, previous)
        cursor = win32gui.GetCursorPos()
        pointer = VirtualPointer()
        pointer.show_idle()
        replies = queue.Queue()

        def read():
            for line in pointer.overlay.stdout:
                replies.put(line.strip())

        threading.Thread(target=read, daemon=True).start()

        def visible(expected):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                pointer.overlay.stdin.write(b"probe\n")
                if replies.get(timeout=8) == expected:
                    return
                time.sleep(0.1)
            pytest.fail("Native overlay did not reach " + repr(expected))

        visible(b"painted")
        assert win32gui.GetForegroundWindow() == hwnd
        assert win32gui.GetCursorPos() == cursor
        desktop = Desktop(guard=lambda: process.alive())
        assert desktop.uia(hwnd, "select", automation_id="Ordinary")["selected"]
        visible(b"hidden")
        assert desktop.uia(hwnd, "select", automation_id="AI")["selected"]
        visible(b"painted")
        tabs.marker_path().unlink()
        visible(b"hidden")
        assert win32gui.GetForegroundWindow() == hwnd
        assert win32gui.GetCursorPos() == cursor
    finally:
        if pointer is not None:
            pointer.close()
        process.close()
