"""Real message routing and overlay; only an owned WinForms fixture is targeted."""

import os
import queue
import subprocess
import threading
import time
from pathlib import Path

from test_uia_provider import ROOT, Client, object_data, receive


def main():
    ps = str(
        Path(os.environ["SystemRoot"])
        / "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    form = subprocess.Popen(
        [
            ps,
            "-NoProfile",
            "-NonInteractive",
            "-Sta",
            "-Command",
            (ROOT / "tests/fixtures/uia_provider.ps1").read_text(encoding="utf-8-sig"),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    rows = queue.Queue()
    reader = threading.Thread(target=receive, args=(form.stdout, rows), daemon=True)
    reader.start()
    client = None

    def event(kind, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            row = rows.get(timeout=max(0.01, end - time.monotonic()))
            if row.get("event") == kind:
                return row
        raise AssertionError("Missing " + kind)

    try:
        ready = event("ready", 20)
        client = Client()

        def pointer(**args):
            return object_data(
                client.rpc("tools/call", dict(name="virtual_pointer", arguments=args))
            )

        win = next(
            w
            for w in object_data(client.tool(action="list"))["windows"]
            if w["pid"] == form.pid and w["title"] == ready["title"]
        )
        wid = win["window_id"]
        object_data(
            client.tool(action="resize_window", window_id=wid, width=700, height=480)
        )
        work = object_data(client.tool(action="displays"))["displays"][0]["work_area"]
        object_data(
            client.tool(
                action="move_window", window_id=wid, x=work[0] + 40, y=work[1] + 40
            )
        )
        for aid, text in [
            ("OrderName", "AI pointer"),
            ("Quantity", "3"),
            ("UnitPrice", "125000"),
        ]:
            object_data(
                client.tool(
                    action="set_value", window_id=wid, automation_id=aid, text=text
                )
            )
        snapshot = object_data(client.tool(action="snapshot", window_id=wid))
        elements = {e.get("automation_id"): e for e in snapshot["elements"]}
        pw = next(
            w
            for w in pointer(action="list")["windows"]
            if w["pid"] == form.pid and w["title"] == ready["title"]
        )
        pid = pw["window_id"]
        x, y, width, height = elements["Calculate"]["bounds"]
        x = int(x + width / 2)
        y = int(y + height / 2)
        before = pointer(action="status")["system_cursor"]
        row = pointer(action="click", window_id=pid, x=x, y=y)
        assert row["physical_input_injected"] is False, row
        assert (
            row["external_input_observed"]
            or row["system_cursor_before"] == row["system_cursor_after"]
        ), row
        assert event("calculate")["output"] == "AI pointer | Tổng tiền: 375000 VND"
        x, y, width, height = elements["DragPad"]["bounds"]
        x = int(x)
        y = int(y)
        pointer(action="click", window_id=pid, x=x + 40, y=y + 30, button="right")
        assert event("mouse_down")["button"] == "Right"
        event("mouse_up")
        pointer(action="click", window_id=pid, x=x + 40, y=y + 30, clicks=2)
        event("native_double_click")
        row = pointer(
            action="drag",
            window_id=pid,
            from_x=x + 30,
            from_y=y + 60,
            x=x + 180,
            y=y + 70,
            duration_ms=100,
        )
        assert (
            row["external_input_observed"]
            or row["system_cursor_before"] == row["system_cursor_after"]
        ), row
        end = time.monotonic() + 5
        while True:
            up = event("mouse_up", max(0.01, end - time.monotonic()))
            if abs(up.get("x", 0) - 180) <= 2:
                break
        pointer(
            action="scroll",
            window_id=pid,
            x=x + 40,
            y=y + 40,
            delta=120,
            horizontal=True,
        )
        event("horizontal_wheel")
        assert pointer(action="status")["overlay_running"]
        assert client.rpc(
            "tools/call",
            dict(
                name="virtual_pointer",
                arguments=dict(action="click", window_id=pid, x=-20000, y=-20000),
            ),
        ).get("isError")
        pointer(action="hide")
        assert not pointer(action="status")["overlay_running"]
        object_data(
            client.tool(action="invoke", window_id=wid, automation_id="CloseTest")
        )
        event("closed")
        assert form.wait(timeout=5) == 0
        print(
            "PASS | Real MCP virtual overlay + button effect, right/double/drag/horizontal wheel; physical cursor unchanged; invalid coordinates refused; owned fixture and overlay closed"
        )
    finally:
        if client:
            try:
                client.rpc(
                    "tools/call",
                    dict(name="virtual_pointer", arguments=dict(action="hide")),
                )
            finally:
                client.close()
        if form.poll() is None:
            form.terminate()
            form.wait(timeout=5)
        reader.join(timeout=2)
        form.stdout.close()


if __name__ == "__main__":
    main()
