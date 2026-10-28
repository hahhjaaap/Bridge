"""Current-user desktop primitives. Caller must enforce a separate owner grant.

Never bypass UIPI, elevate, switch to a secure desktop or attach input queues.
Window IDs are observations with a short lifetime, not arbitrary HWND inputs.
"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import struct
import subprocess
import time
import uuid
import zlib
from ctypes import wintypes as w
from pathlib import Path

from .core import PolicyError
from .desktop_guest import BitmapHeader, InputEvent, KeyInput, MouseInput


class Desktop:
    def __init__(self, guard=None):
        import win32gui
        import win32process

        self.gui = win32gui
        self.process = win32process
        self.windows = {}
        self.guard = guard
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.u.SendInput.argtypes = [w.UINT, ctypes.POINTER(InputEvent), ctypes.c_int]
        self.u.SendInput.restype = w.UINT
        self.u.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.u.OpenInputDesktop.restype = w.HANDLE
        self.u.GetUserObjectInformationW.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        self.u.CloseDesktop.argtypes = [w.HANDLE]

    def interactive(self):
        if self.guard is not None and not self.guard():
            raise PolicyError("Computer access was revoked")
        handle = self.u.OpenInputDesktop(0, False, 1)
        if not handle:
            raise PolicyError("Interactive desktop is unavailable or locked")
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = w.DWORD()
            if (
                not self.u.GetUserObjectInformationW(
                    handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
                )
                or name.value.lower() != "default"
            ):
                raise PolicyError("Secure/locked desktop is not controllable by UDB")
        finally:
            self.u.CloseDesktop(handle)

    def listing(self):
        self.interactive()
        rows = []
        observed = {}

        def visit(hwnd, _):
            if not self.gui.IsWindowVisible(hwnd) or not self.gui.GetWindowText(hwnd):
                return
            pid = self.process.GetWindowThreadProcessId(hwnd)[1]
            wid = uuid.uuid4().hex
            observed[wid] = (
                hwnd,
                pid,
                self.gui.GetClassName(hwnd),
                time.monotonic() + 60,
            )
            rows.append(
                dict(
                    window_id=wid,
                    title=self.gui.GetWindowText(hwnd)[:300],
                    pid=pid,
                    bounds=list(self.gui.GetWindowRect(hwnd)),
                    foreground=hwnd == self.gui.GetForegroundWindow(),
                )
            )

        self.gui.EnumWindows(visit, None)
        self.windows = observed
        return dict(
            windows=rows, expires_in_seconds=60, coordinates="physical screen pixels"
        )

    @staticmethod
    def _chrome_image_path(pid):
        """Resolve a Chrome process image without trusting PATH or a window title."""
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.QueryFullProcessImageNameW.argtypes = [
            w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)
        ]
        kernel.QueryFullProcessImageNameW.restype = w.BOOL
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            size = w.DWORD(32768)
            image = ctypes.create_unicode_buffer(size.value)
            if not kernel.QueryFullProcessImageNameW(
                handle, 0, image, ctypes.byref(size)
            ):
                return None
            path = image.value
            if path.lower().replace("/", "\\").endswith(
                "\\google\\chrome\\application\\chrome.exe"
            ):
                return path
            return None
        finally:
            kernel.CloseHandle(handle)

    def _chrome_windows(self):
        """Observe only top-level windows belonging to real Chrome processes."""
        images = {}
        rows = []

        def visit(hwnd, _):
            if (
                not self.gui.IsWindowVisible(hwnd)
                or self.gui.GetClassName(hwnd) != "Chrome_WidgetWin_1"
            ):
                return
            pid = self.process.GetWindowThreadProcessId(hwnd)[1]
            if pid not in images:
                images[pid] = self._chrome_image_path(pid)
            if images[pid]:
                rows.append((hwnd, pid, images[pid]))

        self.gui.EnumWindows(visit, None)
        return rows

    def open_chrome_tab(self, url):
        """Ask an already-running Chrome to open a visible tab.

        This is explicitly owner-granted computer access, NOT an owned CDP
        session. No existing tab is inspected, adopted, or controlled.
        """
        from .chrome_aura import launch_target
        from .host_browser import validate_url

        self.interactive()
        target = validate_url(url)
        chrome_target, branding_requested = launch_target(target)
        before = self._chrome_windows()
        if not before:
            raise PolicyError(
                "No existing Google Chrome window; no browser was launched"
            )
        foreground = self.gui.GetForegroundWindow()
        chosen = next((row for row in before if row[0] == foreground), before[0])
        try:
            result = subprocess.run(
                [chosen[2], "--new-tab", chrome_target],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                timeout=15,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PolicyError(
                "Chrome did not acknowledge the new-tab request; inspect "
                "the browser before retrying"
            ) from exc
        if result.returncode != 0:
            raise PolicyError(
                "Chrome rejected the new-tab request; inspect before retrying"
            )
        after = self._chrome_windows()
        return dict(
            requested=True,
            chrome_pid=chosen[1],
            chrome_windows_before=len(before),
            chrome_windows_after=len(after),
            existing_window_reused_observed=bool(after)
            and {row[0] for row in before} == {row[0] for row in after},
            branding_requested=branding_requested,
            branding_verified=False,
            dom_attached=False,
            user_input_injected=False,
        )

    def resolve(self, window_id, focus=True):
        self.interactive()
        row = self.windows.get(window_id)
        if not row:
            raise PolicyError("Observe windows with computer_desktop list first")
        hwnd, pid, kind, until = row
        if (
            time.monotonic() > until
            or not self.gui.IsWindow(hwnd)
            or not self.gui.IsWindowVisible(hwnd)
            or self.process.GetWindowThreadProcessId(hwnd)[1] != pid
            or self.gui.GetClassName(hwnd) != kind
        ):
            raise PolicyError("Window observation expired or changed; list again")
        if focus and self.gui.GetForegroundWindow() != hwnd:
            try:
                self.gui.ShowWindow(hwnd, 9)
                self.gui.SetForegroundWindow(hwnd)
            except Exception:
                pass  # Foreground APIs are best-effort; verify focus below.
            if self.gui.GetForegroundWindow() != hwnd:
                self.uia(hwnd, "uia_focus")
        if focus and self.gui.GetForegroundWindow() != hwnd:
            raise PolicyError("Target is not foreground; no input was sent")
        return hwnd

    def send(self, hwnd, events):
        if self.guard is not None and not self.guard():
            raise PolicyError("Computer access was revoked; input stopped")
        if self.gui.GetForegroundWindow() != hwnd:
            raise PolicyError(
                "Foreground changed; input stopped. Inspect before retrying"
            )
        values = (InputEvent * len(events))(*events)
        if self.u.SendInput(len(events), values, ctypes.sizeof(InputEvent)) != len(
            events
        ):
            raise PolicyError(
                "Windows blocked or partially accepted input; inspect before retrying. No elevation/UAC bypass"
            )

    def key(self, vk=0, scan=0, flags=0):
        event = InputEvent()
        event.type = 1
        event.data.ki = KeyInput(vk, scan, flags, 0, 0)
        return event

    def uia(
        self,
        hwnd,
        mode,
        text="",
        automation_id="",
        name="",
        control_type="",
        wait_ms=0,
        condition="exists",
    ):
        self.interactive()
        pid = self.process.GetWindowThreadProcessId(hwnd)[1]
        request = dict(
            hwnd=hwnd,
            pid=pid,
            mode=mode,
            text=text,
            automation_id=automation_id,
            name=name,
            control_type=control_type,
            wait_ms=wait_ms,
            condition=condition,
        )
        exe = (
            Path(os.environ["SystemRoot"])
            / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        helper = Path(__file__).with_name("computer_uia.ps1")
        try:
            result = subprocess.run(
                [
                    str(exe),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(helper),
                ],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=7 + (wait_ms / 1000 if mode == "wait_for" else 0),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                shell=False,
            )
            data = json.loads(result.stdout.strip())
            if result.returncode or not data.get("ok"):
                raise ValueError("UIA declined")
            return data
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise PolicyError(
                "UI Automation was refused or timed out; inspect the target before retrying. No input/elevation fallback was used."
            ) from None

    def dispatch(
        self,
        action,
        window_id="",
        text="",
        x=0,
        y=0,
        key="",
        delta=0,
        automation_id="",
        name="",
        control_type="",
        **extra,
    ):
        self.u.SetThreadDpiAwarenessContext.argtypes = [w.HANDLE]
        self.u.SetThreadDpiAwarenessContext.restype = w.HANDLE
        previous = self.u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not previous:
            raise PolicyError("Per-monitor DPI context unavailable")
        try:
            return self._dispatch(
                action,
                window_id,
                text,
                x,
                y,
                key,
                delta,
                automation_id,
                name,
                control_type,
                **extra,
            )
        finally:
            self.u.SetThreadDpiAwarenessContext(previous)

    def _dispatch(
        self,
        action,
        window_id="",
        text="",
        x=0,
        y=0,
        key="",
        delta=0,
        automation_id="",
        name="",
        control_type="",
        button="left",
        clicks=1,
        from_x=None,
        from_y=None,
        duration_ms=250,
        horizontal=False,
        width=0,
        height=0,
        window_state="",
        wait_ms=1000,
        condition="exists",
        steps=None,
        display_index=-1,
        region=None,
    ):
        self.validate_options(
            action, button, clicks, duration_ms, horizontal, wait_ms, condition
        )
        if action == "list":
            return self.listing()
        if action == "displays":
            self.interactive()
            return self.display_inventory()
        if action == "screenshot" and (display_index != -1 or region is not None):
            self.interactive()
            return self.display_capture(display_index, region)
        if action == "batch":
            return self.batch(window_id, steps)
        if action == "open_chrome_tab":
            return self.open_chrome_tab(text)
        hwnd = self.resolve(
            window_id,
            focus=action
            not in (
                "screenshot",
                "snapshot",
                "set_value",
                "invoke",
                "uia_focus",
                "wait_for",
                "move_window",
                "resize_window",
                "window_state",
            ),
        )
        if action in ("snapshot", "set_value", "invoke", "uia_focus", "wait_for"):
            return self.uia(
                hwnd,
                action,
                text,
                automation_id,
                name,
                control_type,
                wait_ms,
                condition,
            )
        if action in ("move_window", "resize_window", "window_state"):
            return self.window_action(hwnd, action, x, y, width, height, window_state)
        if action == "focus":
            return dict(foreground=True)
        if action == "screenshot":
            return self.screenshot(hwnd)
        self.u.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.u.GetAsyncKeyState.restype = ctypes.c_short
        if any(
            self.u.GetAsyncKeyState(vk) & 0x8000 for vk in (1, 2, 16, 17, 18, 91, 92)
        ):
            raise PolicyError(
                "User input is active; wait until mouse buttons/modifiers are released"
            )
        if action in ("mouse_move", "drag"):
            return self.mouse_action(
                hwnd, action, x, y, from_x, from_y, duration_ms, button
            )
        if action == "type":
            if not isinstance(text, str) or len(text) > 5000:
                raise PolicyError("Type at most 5000 characters per call")
            data = text.encode("utf-16-le")
            for at in range(0, len(data), 64):
                events = []
                for i in range(at, min(at + 64, len(data)), 2):
                    unit = int.from_bytes(data[i : i + 2], "little")
                    events.extend(
                        [self.key(scan=unit, flags=4), self.key(scan=unit, flags=6)]
                    )
                self.send(hwnd, events)
            return dict(injected_characters=len(text), verified_application_text=False)
        if action == "key":
            names = {
                "CTRL": 17,
                "ALT": 18,
                "SHIFT": 16,
                "ENTER": 13,
                "TAB": 9,
                "ESC": 27,
                "BACKSPACE": 8,
                "DELETE": 46,
                "HOME": 36,
                "END": 35,
                "LEFT": 37,
                "UP": 38,
                "RIGHT": 39,
                "DOWN": 40,
                "SPACE": 32,
                "WIN": 91,
                "PAGEUP": 33,
                "PAGEDOWN": 34,
            }
            names.update({chr(n): n for n in range(65, 91)})
            names.update({str(n): 48 + n for n in range(10)})
            names.update({"F" + str(n): 111 + n for n in range(1, 13)})
            parts = key.upper().split("+")
            if not 1 <= len(parts) <= 4 or any(k not in names for k in parts):
                raise PolicyError("Unsupported key chord")
            if set(parts) in ({"WIN", "L"}, {"CTRL", "ALT", "DELETE"}):
                raise PolicyError("Secure/lock key combinations are refused")
            pressed = []
            try:
                for k in parts:
                    vk = names[k]
                    self.send(
                        hwnd,
                        [self.key(vk, flags=1 if vk in (*range(33, 47), 91) else 0)],
                    )
                    pressed.append(vk)
            finally:
                # Release only keys injected by this call, even if foreground changed.
                for vk in reversed(pressed):
                    event = self.key(
                        vk, flags=2 | (1 if vk in (*range(33, 47), 91) else 0)
                    )
                    values = (InputEvent * 1)(event)
                    self.u.SendInput(1, values, ctypes.sizeof(InputEvent))
            return dict(injected_key=key, verified_application_result=False)
        if action in ("click", "scroll"):
            if any(
                isinstance(v, bool) or not isinstance(v, int) for v in (x, y, delta)
            ):
                raise PolicyError("Coordinates/delta must be integers")
            left, top, right, bottom = self.gui.GetWindowRect(hwnd)
            if not left <= x < right or not top <= y < bottom:
                raise PolicyError("Point is outside the observed target window")
            if action == "scroll" and not -1200 <= delta <= 1200:
                raise PolicyError("Scroll delta must be -1200..1200")

            def check_point():
                hit = self.gui.WindowFromPoint((x, y))
                self.u.GetAncestor.argtypes = [w.HWND, w.UINT]
                self.u.GetAncestor.restype = w.HWND
                if not hit or self.u.GetAncestor(hit, 2) != hwnd:
                    raise PolicyError(
                        "Another window covers this point; inspect before clicking or scrolling"
                    )

            check_point()
            import win32api

            win32api.SetCursorPos((x, y))
            down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[button]
            if action == "click":
                flags = [(down, 0), (up, 0)] * clicks
            else:
                flags = [(0x1000 if horizontal else 0x800, delta & 0xFFFFFFFF)]
            events = []
            for flag, data in flags:
                e = InputEvent()
                e.type = 0
                e.data.mi = MouseInput(0, 0, data, flag, 0, 0)
                events.append(e)
            try:
                check_point()
                self.send(hwnd, events)
            except PolicyError:
                if action == "click":
                    e = InputEvent()
                    e.type = 0
                    e.data.mi = MouseInput(0, 0, 0, up, 0, 0)
                    self.u.SendInput(1, (InputEvent * 1)(e), ctypes.sizeof(InputEvent))
                raise
            return dict(injected=True, verified_application_result=False)
        raise PolicyError("Unsupported desktop action")

    @staticmethod
    def validate_options(
        action,
        button="left",
        clicks=1,
        duration_ms=250,
        horizontal=False,
        wait_ms=1000,
        condition="exists",
    ):
        if action not in {
            "list",
            "focus",
            "screenshot",
            "type",
            "key",
            "click",
            "scroll",
            "snapshot",
            "set_value",
            "invoke",
            "uia_focus",
            "mouse_move",
            "drag",
            "move_window",
            "resize_window",
            "window_state",
            "wait_for",
            "batch",
            "displays",
            "open_chrome_tab",
        }:
            raise PolicyError("Unsupported desktop action")
        if (
            button not in ("left", "right", "middle")
            or type(clicks) is not int
            or not 1 <= clicks <= 2
        ):
            raise PolicyError("Button left/right/middle; clicks 1..2")
        if (
            type(duration_ms) is not int
            or not 0 <= duration_ms <= 1500
            or type(horizontal) is not bool
        ):
            raise PolicyError("duration_ms 0..1500; horizontal boolean")
        if (
            type(wait_ms) is not int
            or not 0 <= wait_ms <= 10000
            or condition not in ("exists", "absent", "enabled")
        ):
            raise PolicyError("wait_ms 0..10000; condition exists/absent/enabled")

    def batch(self, window_id, steps):
        if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
            raise PolicyError("Batch needs 1..12 steps")
        import inspect

        fields = set(inspect.signature(self._dispatch).parameters) - {"steps"}
        for row in steps:
            if (
                not isinstance(row, dict)
                or set(row) - fields
                or row.get("action") in (
                    "list", "batch", "screenshot", "displays", "open_chrome_tab"
                )
                or "action" not in row
            ):
                raise PolicyError("Invalid/nested batch step; capture/list separately")
            self.validate_options(
                row["action"],
                **{
                    k: row[k]
                    for k in (
                        "button",
                        "clicks",
                        "duration_ms",
                        "horizontal",
                        "wait_ms",
                        "condition",
                    )
                    if k in row
                },
            )
        deadline = time.monotonic() + 20
        results = []
        for index, row in enumerate(steps):
            try:
                if time.monotonic() > deadline:
                    raise PolicyError("Batch time budget exceeded")
                args = dict(row)
                args.setdefault("window_id", window_id)
                value = self._dispatch(**args)
                results.append(dict(index=index, result=value))
            except (PolicyError, OSError, ValueError, TypeError) as exc:
                return dict(
                    ok=False,
                    completed_steps=len(results),
                    failed_index=index,
                    error=str(exc),
                    results=results,
                    rollback=False,
                )
        return dict(
            ok=True, completed_steps=len(results), results=results, rollback=False
        )

    def window_action(self, hwnd, action, x, y, width, height, state):
        if action == "window_state":
            modes = {"restore": 4, "minimize": 6, "maximize": 3}
            if state not in modes:
                raise PolicyError("window_state restore/minimize/maximize")
            self.gui.ShowWindow(hwnd, modes[state])
        else:
            if self.gui.IsIconic(hwnd):
                raise PolicyError("Restore the observed window before moving/resizing")
            if any(type(v) is not int for v in (x, y, width, height)):
                raise PolicyError("Integer window geometry required")
            left, top, right, bottom = self.gui.GetWindowRect(hwnd)
            box = (
                (x, y, x + right - left, y + bottom - top)
                if action == "move_window"
                else (left, top, left + width, top + height)
            )
            if action == "resize_window" and not (
                96 <= width <= 8192 and 64 <= height <= 8192
            ):
                raise PolicyError("Window dimensions out of range")
            monitors = self.display_inventory()["displays"]
            if not any(
                min(box[2], m["work_area"][2]) - max(box[0], m["work_area"][0]) >= 32
                and min(box[3], m["work_area"][3]) - max(box[1], m["work_area"][1])
                >= 32
                for m in monitors
            ):
                raise PolicyError("Window must remain visible on an active display")
            flags = 0x4 | 0x10 | (0x1 if action == "move_window" else 0x2)
            self.gui.SetWindowPos(hwnd, 0, x, y, width, height, flags)
        return dict(
            bounds=list(self.gui.GetWindowRect(hwnd)),
            minimized=bool(self.gui.IsIconic(hwnd)),
            foreground=hwnd == self.gui.GetForegroundWindow(),
        )

    def checked_point(self, hwnd, x, y):
        if type(x) is not int or type(y) is not int:
            raise PolicyError("Integer physical coordinates required")
        left, top, right, bottom = self.gui.GetWindowRect(hwnd)
        if not left <= x < right or not top <= y < bottom:
            raise PolicyError("Point outside observed window")
        hit = self.gui.WindowFromPoint((x, y))
        self.u.GetAncestor.argtypes = [w.HWND, w.UINT]
        self.u.GetAncestor.restype = w.HWND
        if not hit or self.u.GetAncestor(hit, 2) != hwnd:
            raise PolicyError("Another window covers target point")

    def mouse_action(self, hwnd, action, x, y, from_x, from_y, duration_ms, button):
        import win32api

        self.checked_point(hwnd, x, y)
        down, up = {"left": (2, 4), "right": (8, 16), "middle": (32, 64)}[button]
        pressed = False
        if action == "drag":
            self.checked_point(hwnd, from_x, from_y)
            win32api.SetCursorPos((from_x, from_y))
            event = InputEvent()
            event.type = 0
            event.data.mi = MouseInput(0, 0, 0, down, 0, 0)
            self.send(hwnd, [event])
            pressed = True
            start = (from_x, from_y)
        else:
            start = win32api.GetCursorPos()
        last = start
        try:
            count = max(1, duration_ms // 25)
            for i in range(1, count + 1):
                self.interactive()
                if self.gui.GetForegroundWindow() != hwnd:
                    raise PolicyError("Foreground changed; pointer action stopped")
                actual = win32api.GetCursorPos()
                if i > 1 and (
                    abs(actual[0] - last[0]) > 3 or abs(actual[1] - last[1]) > 3
                ):
                    raise PolicyError("User moved pointer; action stopped")
                point = (
                    round(start[0] + (x - start[0]) * i / count),
                    round(start[1] + (y - start[1]) * i / count),
                )
                if action == "drag":
                    self.checked_point(hwnd, *point)
                win32api.SetCursorPos(point)
                last = point
                if duration_ms:
                    time.sleep(duration_ms / count / 1000)
        finally:
            if pressed:
                event = InputEvent()
                event.type = 0
                event.data.mi = MouseInput(0, 0, 0, up, 0, 0)
                self.u.SendInput(1, (InputEvent * 1)(event), ctypes.sizeof(InputEvent))
        return dict(injected=True, cursor=list(last), verified_application_result=False)

    def display_inventory(self):
        import win32api

        rows = []
        for index, (handle, _, bounds) in enumerate(win32api.EnumDisplayMonitors()):
            info = win32api.GetMonitorInfo(handle)
            dpi = None
            try:
                sh = ctypes.WinDLL("shcore")
                fn = sh.GetDpiForMonitor
                fn.argtypes = [
                    w.HANDLE,
                    ctypes.c_int,
                    ctypes.POINTER(w.UINT),
                    ctypes.POINTER(w.UINT),
                ]
                dx = w.UINT()
                dy = w.UINT()
                if fn(int(handle), 0, ctypes.byref(dx), ctypes.byref(dy)) == 0:
                    dpi = [dx.value, dy.value]
            except (OSError, AttributeError):
                pass
            rows.append(
                dict(
                    index=index,
                    bounds=list(bounds),
                    work_area=list(info["Work"]),
                    primary=bool(info["Flags"] & 1),
                    dpi=dpi,
                    scale=None if dpi is None else [dpi[0] / 96, dpi[1] / 96],
                )
            )
        return dict(displays=rows, coordinates="physical screen pixels")

    def display_capture(self, index, region):
        displays = self.display_inventory()["displays"]
        if region is not None:
            if (
                not isinstance(region, list)
                or len(region) != 4
                or any(type(v) is not int for v in region)
            ):
                raise PolicyError("region is [left,top,right,bottom]")
            left, top, right, bottom = region
            if (
                left >= right
                or top >= bottom
                or not any(
                    left >= m["bounds"][0]
                    and top >= m["bounds"][1]
                    and right <= m["bounds"][2]
                    and bottom <= m["bounds"][3]
                    for m in displays
                )
            ):
                raise PolicyError("Region must fit inside one active display")
            return self.capture(region)
        if type(index) is not int or not 0 <= index < len(displays):
            raise PolicyError("Unknown display index; call displays first")
        return self.capture(displays[index]["bounds"])

    def screenshot(self, hwnd):
        return self.capture(self.gui.GetWindowRect(hwnd))

    def capture(self, box):
        import win32con
        import win32gui
        import win32ui

        left, top, right, bottom = box
        capture_width, capture_height = right - left, bottom - top
        if not 1 <= capture_width <= 16384 or not 1 <= capture_height <= 16384:
            raise PolicyError("Target window bounds exceed the capture limit")
        scale = min(1, 1920 / capture_width, 1440 / capture_height)
        width = max(1, round(capture_width * scale))
        height = max(1, round(capture_height * scale))
        hdc = win32gui.GetDC(None)
        src = win32ui.CreateDCFromHandle(hdc)
        dest = src.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        previous = None
        try:
            bitmap.CreateCompatibleBitmap(src, width, height)
            previous = dest.SelectObject(bitmap)
            g = ctypes.WinDLL("gdi32", use_last_error=True)
            g.StretchBlt.argtypes = [
                w.HDC,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                w.HDC,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                w.DWORD,
            ]
            if not g.StretchBlt(
                dest.GetSafeHdc(),
                0,
                0,
                width,
                height,
                src.GetSafeHdc(),
                left,
                top,
                capture_width,
                capture_height,
                win32con.SRCCOPY | 0x40000000,
            ):
                raise PolicyError("Windows refused the screen capture")
            dest.SelectObject(previous)
            previous = None
            header = BitmapHeader(
                ctypes.sizeof(BitmapHeader),
                width,
                -height,
                1,
                32,
                0,
                width * height * 4,
                0,
                0,
                0,
                0,
            )
            buffer = ctypes.create_string_buffer(width * height * 4)
            g.GetDIBits.argtypes = [
                w.HDC,
                w.HBITMAP,
                w.UINT,
                w.UINT,
                ctypes.c_void_p,
                ctypes.c_void_p,
                w.UINT,
            ]
            if (
                g.GetDIBits(
                    src.GetSafeHdc(),
                    bitmap.GetHandle(),
                    0,
                    height,
                    buffer,
                    ctypes.byref(header),
                    0,
                )
                != height
            ):
                raise PolicyError("Windows refused bitmap readback")
            raw = buffer.raw
            rgb = bytearray(width * height * 3)
            # GetDIBits uses an explicit negative height: top-down BGRX.
            stride = width * 4
            for y in range(height):
                row = raw[y * stride : (y + 1) * stride]
                target = y * width * 3
                rgb[target : target + width * 3 : 3] = row[2::4]
                rgb[target + 1 : target + width * 3 : 3] = row[1::4]
                rgb[target + 2 : target + width * 3 : 3] = row[0::4]
            rows = b"".join(
                b"\0" + rgb[y * width * 3 : (y + 1) * width * 3] for y in range(height)
            )

            def chunk(kind, value):
                return (
                    struct.pack("!I", len(value))
                    + kind
                    + value
                    + struct.pack("!I", zlib.crc32(kind + value) & 0xFFFFFFFF)
                )

            png = (
                b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows, 3))
                + chunk(b"IEND", b"")
            )
            return dict(
                png_base64=base64.b64encode(png).decode(),
                bounds=[left, top, right, bottom],
                image_width=width,
                image_height=height,
                scale_x=capture_width / width,
                scale_y=capture_height / height,
                source="visible_screen_region",
            )
        finally:
            if previous is not None:
                dest.SelectObject(previous)
            win32gui.DeleteObject(bitmap.GetHandle())
            dest.DeleteDC()
            src.DeleteDC()
            win32gui.ReleaseDC(None, hdc)
