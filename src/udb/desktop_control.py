"""Cooperative desktop yielding and a local-only emergency pause.

This does not block human input or provide exclusive desktop ownership. External
programs can still act on the UI; already completed effects are never undone.
"""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes as w

from .core import PolicyError, reject_links
from .host_client import atomic_json

PAUSE_FILE = "desktop-paused.json"
HOTKEY = "Ctrl+Alt+Shift+Backspace"


def pause_local(agent, paused=True):
    """Called by the local UI/hotkey only; never exposed as a remote setter."""
    atomic_json(agent.state / PAUSE_FILE, {"paused": bool(paused)})


def paused(agent):
    path = agent.state / PAUSE_FILE
    reject_links(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(257)
        value = json.loads(raw)
        return (
            len(raw) > 256
            or not isinstance(value, dict)
            or value.get("paused") is not False
        )
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


def browser_handoff(agent):
    path = agent.state / "browser-handoff.json"
    reject_links(path)
    try:
        with path.open("rb") as stream:
            raw = stream.read(257)
        value = json.loads(raw)
        return (
            len(raw) > 256
            or not isinstance(value, dict)
            or value.get("active") is not False
        )
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return True


class WindowsInput:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user.GetLastInputInfo.argtypes = [ctypes.c_void_p]
        self.user.GetLastInputInfo.restype = w.BOOL
        self.user.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
        self.user.GetCursorPos.restype = w.BOOL
        self.user.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user.GetAsyncKeyState.restype = ctypes.c_short
        self.kernel.GetTickCount64.restype = ctypes.c_ulonglong

    def sample(self):
        class LastInput(ctypes.Structure):
            _fields_ = [("size", w.UINT), ("tick", w.DWORD)]

        value = LastInput(ctypes.sizeof(LastInput), 0)
        point = w.POINT()
        if not self.user.GetLastInputInfo(
            ctypes.byref(value)
        ) or not self.user.GetCursorPos(ctypes.byref(point)):
            raise PolicyError("Cannot observe user activity; no desktop input was sent")
        age = ((self.kernel.GetTickCount64() & 0xFFFFFFFF) - value.tick) & 0xFFFFFFFF
        emergency = all(
            self.user.GetAsyncKeyState(vk) & 0x8000 for vk in (17, 18, 16, 8)
        )
        return dict(
            tick=value.tick,
            position=(point.x, point.y),
            idle_seconds=age / 1000,
            emergency=emergency,
        )


class InputControl:
    def __init__(self, agent, observer=None, idle_seconds=2.0):
        self.agent = agent
        self.observer = observer or WindowsInput()
        self.idle_seconds = idle_seconds
        self.injected = None

    def status(self):
        sample = self.observer.sample()
        if sample["emergency"]:
            pause_local(self.agent)
        is_paused = paused(self.agent)
        signature = (sample["tick"], sample["position"])
        remaining = max(0.0, self.idle_seconds - sample["idle_seconds"])
        human_active = signature != self.injected and remaining > 0
        handed_off = browser_handoff(self.agent)
        return dict(
            state="paused"
            if is_paused
            else "browser_handoff"
            if handed_off
            else "user_active"
            if human_active
            else "available",
            paused=is_paused,
            retry_after_seconds=round(remaining, 2) if human_active else 0,
            emergency_hotkey=HOTKEY,
            exclusive=False,
            resume="Use Tiếp tục điều khiển Windows in the local UDB manager"
            if is_paused
            else "",
        )

    def check(self):
        state = self.status()
        if state["state"] == "browser_handoff":
            raise PolicyError(
                "The owner has browser control; return it to background in the local UDB manager"
            )
        if state["state"] != "available":
            raise PolicyError(
                "Desktop input paused locally; resume in the UDB manager"
                if state["paused"]
                else f"User is active; desktop action yielded. Wait {state['retry_after_seconds']}s, observe again; do not replay uncertain actions"
            )

    def note_injected(self):
        sample = self.observer.sample()
        self.injected = (sample["tick"], sample["position"])


class PauseHotkey:
    """Register an emergency chord in the local host process, without hooks."""

    def __init__(self, agent):
        import threading

        self.agent = agent
        self.ready = threading.Event()
        self.available = False
        self.thread_id = None
        self.thread = threading.Thread(
            target=self._run, daemon=True, name="UDB emergency pause"
        )
        self.thread.start()
        self.ready.wait(1)

    def _run(self):
        try:
            self._listen()
        except OSError:
            self.available = False
        finally:
            self.ready.set()

    def _listen(self):
        user = ctypes.WinDLL("user32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentThreadId.restype = w.DWORD
        user.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        user.RegisterHotKey.restype = w.BOOL
        user.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.GetMessageW.restype = ctypes.c_int
        self.thread_id = kernel.GetCurrentThreadId()
        self.available = bool(user.RegisterHotKey(None, 1, 0x4000 | 1 | 2 | 4, 8))
        self.ready.set()
        if not self.available:
            return
        try:
            message = w.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    pause_local(self.agent)
        finally:
            user.UnregisterHotKey(None, 1)
            self.available = False

    def close(self):
        if self.available and self.thread_id:
            user = ctypes.WinDLL("user32", use_last_error=True)
            user.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
            user.PostThreadMessageW(self.thread_id, 0x0012, 0, 0)
        self.thread.join(timeout=2)
