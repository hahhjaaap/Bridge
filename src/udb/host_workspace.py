"""A separate, non-activating native Windows text workspace owned by UDB.

No SendInput, SetCursorPos, SetForegroundWindow, clipboard or user window lookup.
Only this launched process's class/nonce-matched Edit control accepts messages.
"""

from __future__ import annotations

import ctypes
import re
import time

from .core import PolicyError
from .host_browser import OwnedProcess


def run(token):
    if not re.fullmatch("[a-f0-9]{32}", token):
        raise ValueError("Invalid workspace token")
    import win32api
    import win32con
    import win32gui

    instance = win32api.GetModuleHandle(None)
    edit = None

    def procedure(hwnd, message, wp, lp):
        if message == win32con.WM_SIZE and edit:
            win32gui.MoveWindow(
                edit, 16, 16, max(10, (lp & 65535) - 32), max(10, (lp >> 16) - 32), True
            )
        if message == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        if message == win32con.WM_MOUSEACTIVATE:
            return 3  # MA_NOACTIVATE
        return win32gui.DefWindowProc(hwnd, message, wp, lp)

    wc = win32gui.WNDCLASS()
    wc.hInstance = instance
    wc.lpszClassName = "UDBBackgroundWorkspace-" + token
    wc.lpfnWndProc = procedure
    wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    wc.hbrBackground = win32con.COLOR_WINDOW + 1
    win32gui.RegisterClass(wc)
    hwnd = win32gui.CreateWindowEx(
        0x08000000 | 0x00040000,
        wc.lpszClassName,
        "UDB Workspace · " + token[:8],
        win32con.WS_OVERLAPPEDWINDOW,
        100,
        100,
        780,
        500,
        0,
        0,
        instance,
        None,
    )
    edit = win32gui.CreateWindowEx(
        win32con.WS_EX_CLIENTEDGE,
        "Edit",
        "",
        win32con.WS_CHILD
        | win32con.WS_VISIBLE
        | win32con.WS_VSCROLL
        | win32con.ES_MULTILINE
        | win32con.ES_AUTOVSCROLL
        | win32con.ES_WANTRETURN,
        16,
        16,
        740,
        420,
        hwnd,
        1001,
        instance,
        None,
    )
    win32gui.SendMessage(edit, win32con.EM_SETLIMITTEXT, 60000, 0)
    win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    win32gui.UpdateWindow(hwnd)
    win32gui.PumpMessages()


class Workspace:
    def __init__(self, home, token):
        self.proc = None
        self.hwnd = None
        self.edit = None
        self.token = token
        from .launcher import worker_command

        cmd = worker_command("core")
        try:
            self.proc = OwnedProcess(cmd + ["--host-workspace", token], home)
            import win32gui
            import win32process

            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                matches = []

                def visit(hwnd, _, matches=matches):
                    if win32gui.GetClassName(
                        hwnd
                    ) == "UDBBackgroundWorkspace-" + token and self.proc.owns_pid(
                        win32process.GetWindowThreadProcessId(hwnd)[1]
                    ):
                        matches.append(hwnd)

                win32gui.EnumWindows(visit, None)
                if len(matches) == 1:
                    self.hwnd = matches[0]
                    self.edit = win32gui.GetDlgItem(self.hwnd, 1001)
                    if self.edit:
                        break
                if not self.proc.alive():
                    raise PolicyError("Workspace exited during launch")
                time.sleep(0.05)
            if not self.edit:
                raise PolicyError("Workspace startup timed out")
        except BaseException:
            self.close()
            raise

    def validate(self):
        import win32gui
        import win32process

        if not self.proc or not self.proc.alive() or not win32gui.IsWindow(self.hwnd):
            raise PolicyError("Owned workspace has closed")
        if (
            not self.proc.owns_pid(win32process.GetWindowThreadProcessId(self.hwnd)[1])
            or win32gui.GetClassName(self.hwnd)
            != "UDBBackgroundWorkspace-" + self.token
        ):
            raise PolicyError("Owned window identity changed")
        if (
            win32gui.GetParent(self.edit) != self.hwnd
            or win32gui.GetClassName(self.edit) != "Edit"
        ):
            raise PolicyError("Workspace control identity changed")

    def message(self, msg, wp, lp):
        self.validate()
        from ctypes import wintypes as w

        ui = ctypes.WinDLL("user32", use_last_error=True)
        ui.SendMessageTimeoutW.argtypes = [
            w.HWND,
            w.UINT,
            w.WPARAM,
            w.LPARAM,
            w.UINT,
            w.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        ui.SendMessageTimeoutW.restype = w.LPARAM
        result = ctypes.c_size_t()
        if not ui.SendMessageTimeoutW(
            self.edit, msg, wp, lp, 2, 2000, ctypes.byref(result)
        ):
            raise PolicyError("Owned workspace is unresponsive")
        return result.value

    def read(self):
        self.validate()
        buf = ctypes.create_unicode_buffer(60001)
        self.message(0xD, len(buf), ctypes.addressof(buf))
        return dict(text=buf.value, scope="owned_native_window", no_activate=True)

    def set_text(self, value):
        if not isinstance(value, str) or len(value) > 30000 or "\0" in value:
            raise PolicyError("Workspace text limit: 30000 characters, no NUL")
        buf = ctypes.create_unicode_buffer(value)
        self.message(0xC, 0, ctypes.addressof(buf))
        if self.read()["text"] != value:
            raise PolicyError("Workspace text readback mismatch")
        return dict(ok=True, characters=len(value), no_activate=True)

    def close(self):
        if self.proc:
            self.proc.close()
            self.proc = None
