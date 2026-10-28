"""Visual AI cursor plus Win32 message routing. Never inject system input."""

import ctypes
import os
import queue
import threading
import time
import uuid
from ctypes import wintypes as w
from pathlib import Path

from udb.companion.processes import ManagedProcess


class VirtualPointer:
    def __init__(self):
        if os.name != "nt":
            raise RuntimeError("Windows required")
        self.lock = threading.RLock()
        self.windows = {}
        self.overlay = None
        self.position = None
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)

        def api(lib, name, rest, *args):
            f = getattr(lib, name)
            f.restype = rest
            f.argtypes = list(args)
            return f

        api(self.u, "SetThreadDpiAwarenessContext", w.HANDLE, w.HANDLE)
        api(
            self.u,
            "EnumWindows",
            w.BOOL,
            ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM),
            w.LPARAM,
        )
        for name in ["IsWindow", "IsWindowVisible", "IsWindowEnabled", "IsIconic"]:
            api(self.u, name, w.BOOL, w.HWND)
        api(
            self.u, "GetWindowThreadProcessId", w.DWORD, w.HWND, ctypes.POINTER(w.DWORD)
        )
        api(self.u, "GetForegroundWindow", w.HWND)
        api(self.u, "GetAncestor", w.HWND, w.HWND, w.UINT)
        api(self.u, "GetClassNameW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        api(self.u, "GetWindowTextW", ctypes.c_int, w.HWND, w.LPWSTR, ctypes.c_int)
        api(self.u, "GetWindowRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
        api(self.u, "GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))
        api(self.u, "ScreenToClient", w.BOOL, w.HWND, ctypes.POINTER(w.POINT))
        api(self.u, "ChildWindowFromPointEx", w.HWND, w.HWND, w.POINT, w.UINT)
        api(
            self.u,
            "SendMessageTimeoutW",
            ctypes.c_size_t,
            w.HWND,
            w.UINT,
            w.WPARAM,
            w.LPARAM,
            w.UINT,
            w.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        )
        api(self.u, "GetCursorPos", w.BOOL, ctypes.POINTER(w.POINT))
        api(self.u, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
        api(self.u, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
        api(self.u, "GetLastInputInfo", w.BOOL, ctypes.c_void_p)
        api(self.u, "OpenInputDesktop", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        api(
            self.u,
            "GetUserObjectInformationW",
            w.BOOL,
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        )
        api(self.u, "CloseDesktop", w.BOOL, w.HANDLE)
        api(self.k, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
        api(self.k, "CloseHandle", w.BOOL, w.HANDLE)
        api(self.k, "GetProcessTimes", w.BOOL, w.HANDLE, *([ctypes.c_void_p] * 4))

    def interactive(self):
        h = self.u.OpenInputDesktop(0, False, 1)
        if not h:
            raise RuntimeError("Input desktop unavailable")
        try:
            name = ctypes.create_unicode_buffer(256)
            needed = w.DWORD()
            if (
                not self.u.GetUserObjectInformationW(
                    h, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
                )
                or name.value.lower() != "default"
            ):
                raise RuntimeError("Secure desktop refused")
        finally:
            self.u.CloseDesktop(h)

    def identity(self, hwnd):
        pid = w.DWORD()
        self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = self.k.OpenProcess(0x1000, False, pid.value)
        if not h:
            raise RuntimeError("Window process cannot be verified")
        try:
            times = (ctypes.c_uint64 * 4)()
            if not self.k.GetProcessTimes(
                h, *[ctypes.byref(times, i * 8) for i in range(4)]
            ):
                raise RuntimeError("Process identity unavailable")
            name = ctypes.create_unicode_buffer(256)
            self.u.GetClassNameW(hwnd, name, 256)
            return pid.value, int(times[0]), name.value
        finally:
            self.k.CloseHandle(h)

    def list(self):
        observed = {}
        rows = []

        @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        def visit(hwnd, _):
            if not self.u.IsWindowVisible(hwnd):
                return True
            title = ctypes.create_unicode_buffer(301)
            self.u.GetWindowTextW(hwnd, title, 301)
            if not title.value:
                return True
            try:
                identity = self.identity(hwnd)
            except (OSError, RuntimeError):
                return True
            wid = uuid.uuid4().hex
            observed[wid] = (hwnd, identity, time.monotonic() + 60)
            rect = w.RECT()
            self.u.GetWindowRect(hwnd, ctypes.byref(rect))
            rows.append(
                dict(
                    window_id=wid,
                    title=title.value,
                    pid=identity[0],
                    bounds=[rect.left, rect.top, rect.right, rect.bottom],
                )
            )
            return True

        self.u.EnumWindows(visit, 0)
        self.windows = observed
        return dict(windows=rows, expires_in_seconds=60)

    def resolve(self, wid):
        if wid not in self.windows:
            raise ValueError("List windows first")
        hwnd, identity, until = self.windows[wid]
        if (
            time.monotonic() > until
            or not self.u.IsWindow(hwnd)
            or not self.u.IsWindowVisible(hwnd)
            or self.u.IsIconic(hwnd)
            or not self.u.IsWindowEnabled(hwnd)
            or self.identity(hwnd) != identity
        ):
            raise ValueError("Window observation expired or unavailable")
        return hwnd

    def target(self, hwnd, x, y):
        if type(x) is not int or type(y) is not int:
            raise ValueError("Integer screen coordinates required")
        point = w.POINT(x, y)
        if not self.u.ScreenToClient(hwnd, ctypes.byref(point)):
            raise RuntimeError("Coordinate mapping failed")
        rect = w.RECT()
        self.u.GetClientRect(hwnd, ctypes.byref(rect))
        if not 0 <= point.x < rect.right or not 0 <= point.y < rect.bottom:
            raise ValueError(
                "Point outside observed client area: screen="
                + str([x, y])
                + " client="
                + str([point.x, point.y, rect.right, rect.bottom])
            )
        root = hwnd
        for _ in range(12):
            child = self.u.ChildWindowFromPointEx(hwnd, point, 1 | 2 | 4)
            if not child or child == hwnd:
                break
            hwnd = child
            point = w.POINT(x, y)
            self.u.ScreenToClient(hwnd, ctypes.byref(point))
        if self.u.GetAncestor(hwnd, 2) != root:
            raise RuntimeError("Target left the observed window")
        if not -32768 <= point.x <= 32767 or not -32768 <= point.y <= 32767:
            raise ValueError("Coordinate range exceeded")
        return hwnd, (point.x & 65535) | ((point.y & 65535) << 16)

    def show(self, x, y):
        if self.overlay is None or self.overlay.poll() is not None:
            ps = str(
                Path(os.environ["SystemRoot"])
                / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            script = (Path(__file__).with_name("udb_pointer_overlay.ps1")).read_text(
                encoding="utf-8"
            )
            self.overlay = ManagedProcess(
                [ps, "-NoProfile", "-NonInteractive", "-Sta", "-Command", script],
                str(Path(__file__).parent),
                256,
            )
            rows = queue.Queue()

            def read():
                try:
                    rows.put(self.overlay.stdout.readline(512))
                except Exception:
                    rows.put(b"")

            threading.Thread(target=read, daemon=True).start()
            try:
                ready = rows.get(timeout=12)
            except queue.Empty:
                self.close()
                raise RuntimeError("Overlay startup timed out") from None
            if ready.strip() != b"ready":
                self.close()
                raise RuntimeError("Overlay did not start")
        self.overlay.stdin.write((str(x) + "," + str(y) + "\n").encode())
        self.position = [x, y]

    def show_idle(self):
        """Prepare the independent pointer, visible only in selected UDB Chrome tabs.

        A native overlay watches registered Chrome tab UIA IDs. It hides the
        pointer on all ordinary tabs and apps, without moving the real cursor.
        """
        self.interactive()
        left = self.u.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        top = self.u.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        width = self.u.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = self.u.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 0 or height <= 0:
            raise RuntimeError("Virtual screen dimensions unavailable")
        x = left + max(0, min(width - 1, round(width * 0.68)))
        y = top + max(0, min(height - 1, round(height * 0.68)))
        self.show(x, y)
        return dict(
            visibility="selected_udb_chrome_tab_only",
            position=self.position,
            independent=True,
        )

    def close(self):
        if self.overlay:
            self.overlay.close()
            for stream in (self.overlay.stdin, self.overlay.stdout):
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            self.overlay = None

    def cursor(self):
        p = w.POINT()
        self.u.GetCursorPos(ctypes.byref(p))
        return [p.x, p.y]

    def input_stamp(self):
        class Info(ctypes.Structure):
            _fields_ = [("size", w.UINT), ("time", w.DWORD)]

        info = Info()
        info.size = ctypes.sizeof(info)
        return int(info.time) if self.u.GetLastInputInfo(ctypes.byref(info)) else None

    def send(self, hwnd, msg, wp, lp):
        result = ctypes.c_size_t()
        if not self.u.SendMessageTimeoutW(
            hwnd, msg, wp, lp, 2 | 0x20, 150, ctypes.byref(result)
        ):
            raise RuntimeError(
                "Message refused/timed out; outcome unknown, inspect app before retrying"
            )

    def call(self, **args):
        with self.lock:
            previous = self.u.SetThreadDpiAwarenessContext(w.HANDLE(-4))
            if not previous:
                raise RuntimeError("Physical-pixel DPI context unavailable")
            try:
                return self.dispatch(**args)
            finally:
                self.u.SetThreadDpiAwarenessContext(previous)

    def dispatch(
        self,
        action,
        window_id="",
        x=None,
        y=None,
        from_x=None,
        from_y=None,
        button="left",
        clicks=1,
        delta=120,
        horizontal=False,
        duration_ms=250,
    ):
        with self.lock:
            if action in ("hide", "stop"):
                self.close()
                return dict(stopped=True)
            self.interactive()
            if action == "show":
                return self.show_idle()
            if action == "status":
                return dict(
                    position=self.position,
                    overlay_running=self.overlay is not None
                    and self.overlay.poll() is None,
                    method="win32_messages",
                    system_cursor=self.cursor(),
                )
            if action == "list":
                return self.list()
            if action not in ("move", "click", "drag", "scroll"):
                raise ValueError("Action list/status/show/hide/move/click/drag/scroll")
            if (
                button not in ("left", "right", "middle")
                or type(clicks) is not int
                or clicks not in (1, 2)
            ):
                raise ValueError("Invalid mouse button/click count")
            if (
                type(duration_ms) is not int
                or not 0 <= duration_ms <= 1500
                or type(delta) is not int
                or not -1200 <= delta <= 1200
            ):
                raise ValueError("Bounded duration/delta required")
            if any(self.u.GetAsyncKeyState(k) & 0x8000 for k in (1, 2, 4)):
                raise RuntimeError("User holds a mouse button; try after release")
            root = self.resolve(window_id)
            target, lp = self.target(root, x, y)
            before = self.cursor()
            stamp = self.input_stamp()
            foreground = self.u.GetForegroundWindow()
            if action == "drag":
                initial, ilp = self.target(root, from_x, from_y)
                if target != initial:
                    raise ValueError("Drag must stay inside one child control")
            self.show(x, y)
            if action == "move":
                self.send(target, 0x200, 0, lp)
            elif action == "scroll":
                if not -32768 <= x <= 32767 or not -32768 <= y <= 32767:
                    raise ValueError(
                        "Wheel screen coordinates exceed signed 16-bit range"
                    )
                self.send(
                    target,
                    0x20E if horizontal else 0x20A,
                    (delta & 65535) << 16,
                    (x & 65535) | ((y & 65535) << 16),
                )
            else:
                down, up, double, mask = {
                    "left": (0x201, 0x202, 0x203, 1),
                    "right": (0x204, 0x205, 0x206, 2),
                    "middle": (0x207, 0x208, 0x209, 16),
                }[button]
                try:
                    self.resolve(window_id)
                    self.send(target, 0x200, 0, ilp if action == "drag" else lp)
                    self.send(target, down, mask, ilp if action == "drag" else lp)
                    if action == "drag":
                        for i in range(1, 11):
                            self.interactive()
                            self.resolve(window_id)
                            if any(
                                self.u.GetAsyncKeyState(k) & 0x8000 for k in (1, 2, 4)
                            ):
                                raise RuntimeError(
                                    "User mouse button activity interrupted AI gesture"
                                )
                            px = round(from_x + (x - from_x) * i / 10)
                            py = round(from_y + (y - from_y) * i / 10)
                            child, step = self.target(root, px, py)
                            if child != target:
                                raise RuntimeError("Drag crossed a child control")
                            self.show(px, py)
                            self.send(target, 0x200, mask, step)
                            time.sleep(duration_ms / 10000)
                    self.send(target, up, 0, lp)
                    if clicks == 2 and action == "click":
                        self.send(target, double, mask, lp)
                        self.send(target, up, 0, lp)
                except Exception:
                    try:
                        self.send(target, up, 0, lp)
                    except Exception:
                        pass  # Best-effort button release; preserve the original gesture error.
                    raise
            return dict(
                dispatched=True,
                app_effect_verified=False,
                method="win32_messages",
                physical_input_injected=False,
                system_cursor_before=before,
                system_cursor_after=self.cursor(),
                external_input_observed=stamp != self.input_stamp(),
                focus_changed=foreground != self.u.GetForegroundWindow(),
                position=self.position,
            )
