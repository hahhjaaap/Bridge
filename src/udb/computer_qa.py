"""Fixed native primitives on a disposable fixture; NEVER creates an access grant.

No supplied command, path, HWND, app title or input text can enter this self-test.
Only its own Job-owned text window is observed/controlled, never user windows.
"""

from __future__ import annotations

import base64
import os
import tempfile
import time
import uuid
from pathlib import Path

from .core import Agent, PolicyError, reject_links


def run(home):
    home = Path(home).resolve()
    reject_links(home)
    if (
        os.name != "nt"
        or not home.parent.name.startswith("udb-computerqa-")
        or not home.is_relative_to(Path(tempfile.gettempdir()).resolve())
        or not (home / "QA-ISOLATED").is_file()
    ):
        raise PolicyError(
            "Computer self-test requires a disposable fixture under the Windows temp directory"
        )
    from .computer_access import NAME

    if (home / "state" / NAME).exists():
        raise PolicyError("Self-test refuses a home containing a computer access grant")
    import win32api
    import win32con
    import win32crypt
    import win32gui
    import win32process

    from .computer_access import ENTROPY, identity
    from .computer_desktop import Desktop
    from .computer_files import Files
    from .computer_jobs import CommandProcess, Jobs
    from .host_workspace import Workspace

    sample = b"UDB synthetic DPAPI test, not a permission"
    protected = win32crypt.CryptProtectData(
        sample, "UDB synthetic self-test", ENTROPY, None, None, 1
    )
    assert win32crypt.CryptUnprotectData(protected, ENTROPY, None, None, 1)[1] == sample
    assert identity().startswith("S-1-")
    agent = Agent(home)
    work = home / "projects" / "qa"
    work.mkdir(parents=True)
    files = Files(agent)
    p = work / "unicode.txt"
    first = files.dispatch(
        "write", str(p), content="UDB tiếng Việt", expected_sha256="NEW"
    )
    assert files.dispatch("read", str(p))["content"] == "UDB tiếng Việt"
    files.dispatch("write", str(p), content="updated", expected_sha256=first["sha256"])
    powershell = (
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    script = "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);[IO.File]::WriteAllText((Join-Path (Get-Location) 'generated.txt'),'UDB_COMMAND_FILE_OK',[Text.UTF8Encoding]::new($false));Write-Output 'UDB tiếng Việt command OK'"
    # Exercise the real launcher validation/supervisor too: direct native
    # CreateProcess alone misses Windows system executable hardlink semantics.
    jobs = Jobs(agent, lambda: "fixed-isolated-qa")
    try:
        job = jobs.start(
            str(powershell),
            ["-NoProfile", "-NonInteractive", "-Command", script],
            str(work),
            20,
            "fixed-isolated-qa",
        )["job_id"]
        deadline = time.monotonic() + 20
        while (
            jobs.process("read", job)["status"] == "running"
            and time.monotonic() < deadline
        ):
            time.sleep(0.1)
        result = jobs.process("read", job)
        assert result["status"] == "exited" and result["exit_code"] == 0, (
            "Fixed test command failed or did not exit"
        )
        assert "UDB tiếng Việt command OK" in result["output"]
        assert (work / "generated.txt").read_text() == "UDB_COMMAND_FILE_OK"
    finally:
        jobs.close()
    # Verify kill-on-close against a duplicate HANDLE, not a potentially reused PID.
    sleepy = CommandProcess(
        [
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Start-Sleep -Seconds 30",
        ],
        work,
        work / "sleep.log",
    )
    duplicate = win32api.DuplicateHandle(
        win32api.GetCurrentProcess(),
        sleepy.handle,
        win32api.GetCurrentProcess(),
        0,
        False,
        2,
    )
    try:
        sleepy.close()
        import win32event

        assert win32event.WaitForSingleObject(duplicate, 5000) == 0
    finally:
        duplicate.Close()
    text_window = Workspace(home, uuid.uuid4().hex)
    before_cursor = None
    injected_cursor = None
    dpi_context = None
    desktop = None
    try:
        text_window.validate()
        # Constructor can observe the child before SW_SHOWNOACTIVATE runs.
        deadline = time.monotonic() + 3
        while (
            not win32gui.IsWindowVisible(text_window.hwnd)
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        desktop = Desktop(guard=lambda: True)
        pid = win32process.GetWindowThreadProcessId(text_window.hwnd)[1]
        assert text_window.proc.owns_pid(pid)
        # The legacy background fixture deliberately has WS_EX_NOACTIVATE.
        # Remove that flag ONLY on this verified Job-owned disposable window
        # so the input test uses an ordinary activatable application window.
        style = win32gui.GetWindowLong(text_window.hwnd, win32con.GWL_EXSTYLE)
        win32gui.SetWindowLong(
            text_window.hwnd, win32con.GWL_EXSTYLE, style & ~0x08000000
        )
        win32gui.SetWindowPos(
            text_window.hwnd,
            0,
            0,
            0,
            0,
            0,
            win32con.SWP_NOMOVE
            | win32con.SWP_NOSIZE
            | win32con.SWP_NOZORDER
            | win32con.SWP_NOACTIVATE
            | win32con.SWP_FRAMECHANGED,
        )
        wid = uuid.uuid4().hex
        # Observe only the known owned fixture; never enumerate other app titles.
        desktop.windows[wid] = (
            text_window.hwnd,
            pid,
            win32gui.GetClassName(text_window.hwnd),
            time.monotonic() + 60,
        )
        import ctypes

        # Bind physical coordinates for the fixture; not a process-wide DPI change.
        desktop.u.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        desktop.u.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        dpi_context = desktop.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        before_cursor = win32gui.GetCursorPos()
        tree = desktop.dispatch("snapshot", window_id=wid)
        assert any(e.get("automation_id") == "1001" for e in tree["elements"])
        text = "UDB Unicode — tiếng Việt 🦈"
        value = desktop.dispatch(
            "set_value", window_id=wid, text=text, automation_id="1001"
        )
        assert value["verified_control_value"] and text_window.read()["text"] == text
        input_verified = False
        input_blocked = None
        try:
            desktop.dispatch("focus", window_id=wid)
        except PolicyError as exc:
            input_blocked = str(exc)
        if input_blocked is None:
            desktop.dispatch("set_value", window_id=wid, text="", automation_id="1001")
            left, top, right, bottom = win32gui.GetWindowRect(text_window.edit)
            injected_cursor = (left + 20, top + 25)
            desktop.dispatch(
                "click", window_id=wid, x=injected_cursor[0], y=injected_cursor[1]
            )
            desktop.dispatch("type", window_id=wid, text=text)
            deadline = time.monotonic() + 3
            while text_window.read()["text"] != text and time.monotonic() < deadline:
                time.sleep(0.05)
            assert text_window.read()["text"] == text, (
                "SendInput was not reflected in the owned edit control"
            )
            input_verified = True
        image = desktop.dispatch("screenshot", window_id=wid)
        assert base64.b64decode(image["png_base64"]).startswith(b"\x89PNG\r\n\x1a\n")
        assert image["image_width"] <= 1920 and image["scale_x"] >= 1
    finally:
        if (
            before_cursor is not None
            and injected_cursor is not None
            and win32gui.GetCursorPos() == injected_cursor
        ):
            win32api.SetCursorPos(before_cursor)
        text_window.close()
        if dpi_context is not None:
            desktop.u.SetThreadDpiAwarenessContext(dpi_context)
    assert not (agent.state / NAME).exists()
    return dict(
        computer_primitives="PASS",
        files_sha_recovery=True,
        unicode_command_output=True,
        command_generated_file=True,
        owned_job_kill=True,
        owned_window_click=input_verified,
        unicode_sendinput_readback=input_verified,
        input_blocked_reason=input_blocked,
        window_screenshot_png=True,
        grant_created=False,
        uia_snapshot=True,
        background_control_value_verified=True,
        user_window_content_read=False,
        only_owned_fixture_targeted=True,
        synthetic_dpapi_roundtrip=True,
    )
