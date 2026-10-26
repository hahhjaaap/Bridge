"""Guest-only desktop worker. Refuses to inject input on the Windows host.

No network listener, host API, clipboard API, model API or external dependency.
The host maps command input read-only and a separate reply directory writable.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import queue
import re
import socket
import struct
import subprocess
import threading
import time
import uuid
import zlib
from ctypes import wintypes as w
from pathlib import Path

INPUT = Path(r"C:\UDBDesktopInput")
OUTPUT = Path(r"C:\UDBDesktopOutput")


def _system_executable(name: str) -> str:
    """Resolve only the guest's required Windows tools without searching PATH."""
    normalized = name.casefold()
    if normalized not in {"powershell.exe", "shutdown.exe"}:
        raise ValueError("Windows executable is not allowlisted")
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if normalized == "powershell.exe":
        path = root / "System32" / "WindowsPowerShell" / "v1.0" / normalized
    else:
        path = root / "System32" / normalized
    if not path.is_file():
        raise FileNotFoundError(f"Required Windows executable is missing: {normalized}")
    return str(path)


DEMO = r"""Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$f=New-Object Windows.Forms.Form
$f.Text='UDB Sandbox Pad - guest only'
$f.Size=New-Object Drawing.Size(780,480)
$f.StartPosition='CenterScreen'
$f.BackColor=[Drawing.Color]::FromArgb(18,28,22)
$f.ForeColor=[Drawing.Color]::White
$f.Font=New-Object Drawing.Font('Segoe UI',13)
$title=New-Object Windows.Forms.Label
$title.Text='AI mouse and keyboard / Windows Sandbox'
$title.SetBounds(28,24,710,38)
$f.Controls.Add($title)
$hint=New-Object Windows.Forms.Label
$hint.Text='This app is inside the guest VM. Type a message, then click Confirm.'
$hint.SetBounds(28,78,710,52)
$f.Controls.Add($hint)
$box=New-Object Windows.Forms.TextBox
$box.Multiline=$true
$box.SetBounds(28,142,710,110)
$f.Controls.Add($box)
$button=New-Object Windows.Forms.Button
$button.Text='Confirm'
$button.SetBounds(28,278,180,48)
$button.BackColor=[Drawing.Color]::FromArgb(67,99,77)
$f.Controls.Add($button)
$result=New-Object Windows.Forms.Label
$result.Text='Waiting for input...'
$result.SetBounds(28,350,710,54)
$f.Controls.Add($result)
$button.Add_Click({$result.Text='CONFIRMED: '+$box.Text; [IO.File]::WriteAllText('C:\UDBDesktopOutput\demo-result.txt',$box.Text,[Text.UTF8Encoding]::new($false))})

# This timer runs on the WinForms UI thread of UDB Sandbox Pad itself.
# It is NOT keyboard injection and cannot control arbitrary guest applications.
$padSession=(Get-Content -LiteralPath 'C:\UDBDesktopInput\session.json' -Raw | ConvertFrom-Json).session_id
$padOutput='C:\UDBDesktopOutput'
$padTimer=New-Object System.Windows.Forms.Timer
$padTimer.Interval=120
$padTimer.Add_Tick({
  foreach($requestFile in @(Get-ChildItem -LiteralPath $padOutput -Filter 'padreq-*.json' -File -ErrorAction SilentlyContinue)){
    $rid=$requestFile.BaseName.Substring(7)
    if($rid -notmatch '^[a-f0-9]{32}$'){continue}
    $ackFile=Join-Path $padOutput ('padack-'+$rid+'.json')
    $ack=@{session_id=$padSession;id=$rid;ok=$false;action='';result=''}
    try {
      if($requestFile.Length -gt 16000){throw 'Oversized demo input'}
      $request=Get-Content -LiteralPath $requestFile.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
      if($request.session_id -ne $padSession -or $request.id -ne $rid){throw 'Bad guest session or request ID'}
      if($request.mode -eq 'type'){
        if($null -eq $request.text -or [string]$request.text -match "\`0" -or ([string]$request.text).Length -gt 2000){throw 'Invalid demo text'}
        $box.Text=[string]$request.text
        $box.SelectionStart=$box.Text.Length
        $box.ScrollToCaret()
        $ack.action='type';$ack.result='PAD_TEXT_SET'
      } elseif($request.mode -eq 'click'){
        if($null -eq $request.x -or $null -eq $request.y){throw 'Demo pointer required'}
        $pos=[Drawing.Point]::new([int]$request.x,[int]$request.y)
        if(-not $button.RectangleToScreen($button.ClientRectangle).Contains($pos)){throw 'Point outside demo Confirm button'}
        $button.PerformClick()
        $ack.action='click';$ack.result='PAD_CONFIRM_INVOKED'
      } else {throw 'Unsupported Pad operation'}
      $ack.ok=$true
    } catch {
      $ack.result=[string]$_.Exception.Message
    } finally {
      try {
        $temp=$ackFile+'.tmp'
        [IO.File]::WriteAllText($temp,($ack|ConvertTo-Json -Compress),[Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temp -Destination $ackFile -Force
      } catch {}
      Remove-Item -LiteralPath $requestFile.FullName -Force -ErrorAction SilentlyContinue
    }
  }
})
$padTimer.Start()
$f.Add_Shown({$f.Activate();$box.Focus()})
[Windows.Forms.Application]::Run($f)
"""


def assert_guest(config):
    if os.name != "nt":
        raise RuntimeError("Desktop worker runs only inside Windows Sandbox")
    # Query the OS identity, not a caller-controlled USERNAME environment value.
    name = ctypes.create_unicode_buffer(256)
    length = w.DWORD(len(name))
    if not ctypes.windll.advapi32.GetUserNameW(name, ctypes.byref(length)):
        raise ctypes.WinError()
    if name.value.casefold() != "wdagutilityaccount":
        raise RuntimeError("Input refused: Windows Sandbox guest identity required")
    if (
        not config.get("guest_only")
        or socket.gethostname().casefold() == config["host_name"].casefold()
    ):
        raise RuntimeError("Input refused: distinct guest machine required")
    if not re.fullmatch("[a-f0-9]{32}", config.get("session_id", "")):
        raise RuntimeError("Invalid Sandbox session")


class MouseInput(ctypes.Structure):
    _fields_ = [
        ("dx", w.LONG),
        ("dy", w.LONG),
        ("mouseData", w.DWORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class KeyInput(ctypes.Structure):
    _fields_ = [
        ("wVk", w.WORD),
        ("wScan", w.WORD),
        ("dwFlags", w.DWORD),
        ("time", w.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class HardwareInput(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class InputUnion(ctypes.Union):
    _fields_ = [("mi", MouseInput), ("ki", KeyInput), ("hi", HardwareInput)]


class InputEvent(ctypes.Structure):
    _fields_ = [("type", w.DWORD), ("data", InputUnion)]


class BitmapHeader(ctypes.Structure):
    _fields_ = [
        ("size", w.DWORD),
        ("width", w.LONG),
        ("height", w.LONG),
        ("planes", w.WORD),
        ("bit_count", w.WORD),
        ("compression", w.DWORD),
        ("size_image", w.DWORD),
        ("xppm", w.LONG),
        ("yppm", w.LONG),
        ("used", w.DWORD),
        ("important", w.DWORD),
    ]


class GuestDesktop:
    def __init__(self, config):
        assert_guest(config)  # Must precede loading any input-capable interface.
        self.session_id = config["session_id"]
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.g = ctypes.WinDLL("gdi32", use_last_error=True)
        self.bind_input_desktop()
        self.u.SetProcessDPIAware()
        self.u.SendInput.argtypes = [w.UINT, ctypes.POINTER(InputEvent), ctypes.c_int]
        self.u.SendInput.restype = w.UINT
        self.u.GetDC.argtypes = [w.HWND]
        self.u.GetDC.restype = w.HDC
        self.u.ReleaseDC.argtypes = [w.HWND, w.HDC]
        self.u.GetForegroundWindow.restype = w.HWND
        self.u.FindWindowW.argtypes = [w.LPCWSTR, w.LPCWSTR]
        self.u.FindWindowW.restype = w.HWND
        self.u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
        self.u.SetForegroundWindow.argtypes = [w.HWND]
        self.u.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
        self.g.CreateCompatibleDC.argtypes = [w.HDC]
        self.g.CreateCompatibleDC.restype = w.HDC
        self.g.CreateCompatibleBitmap.argtypes = [w.HDC, ctypes.c_int, ctypes.c_int]
        self.g.CreateCompatibleBitmap.restype = w.HBITMAP
        self.g.SelectObject.argtypes = [w.HDC, w.HANDLE]
        self.g.SelectObject.restype = w.HANDLE
        self.g.BitBlt.argtypes = [
            w.HDC,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.HDC,
            ctypes.c_int,
            ctypes.c_int,
            w.DWORD,
        ]
        self.g.GetDIBits.argtypes = [
            w.HDC,
            w.HBITMAP,
            w.UINT,
            w.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            w.UINT,
        ]
        self.g.DeleteObject.argtypes = [w.HANDLE]
        self.g.DeleteDC.argtypes = [w.HDC]

    def object_name(self, handle):
        name = ctypes.create_unicode_buffer(256)
        needed = w.DWORD()
        if not self.u.GetUserObjectInformationW(
            handle, 2, name, ctypes.sizeof(name), ctypes.byref(needed)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return name.value

    def bind_input_desktop(self):
        # Logon workers can inherit a restricted desktop/window-station handle.
        # Reopen this guest user's interactive station using its existing ACL;
        # no ACL, token, elevation, secure desktop or host state is modified.
        self.u.OpenWindowStationW.argtypes = [w.LPCWSTR, w.BOOL, w.DWORD]
        self.u.OpenWindowStationW.restype = w.HANDLE
        self.u.SetProcessWindowStation.argtypes = [w.HANDLE]
        self.u.SetProcessWindowStation.restype = w.BOOL
        self.u.GetUserObjectInformationW.argtypes = [
            w.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
        ]
        self.u.OpenInputDesktop.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.u.OpenInputDesktop.restype = w.HANDLE
        self.u.SetThreadDesktop.argtypes = [w.HANDLE]
        self.u.SetThreadDesktop.restype = w.BOOL
        station = self.u.OpenWindowStationW("WinSta0", False, 0x037F)
        if not station:
            raise RuntimeError(
                "Guest WinSta0 access unavailable: "
                + str(ctypes.WinError(ctypes.get_last_error()))
            )
        if not self.u.SetProcessWindowStation(station):
            raise ctypes.WinError(ctypes.get_last_error())
        self.u.CloseDesktop.argtypes = [w.HANDLE]
        self.station_handle = station
        self.desktop_handle = None
        self.context = {"window_station": self.object_name(station)}
        self.refresh_input_desktop()

    def refresh_input_desktop(self):
        # A restored/reconnected Sandbox client can change the active desktop.
        desktop = self.u.OpenInputDesktop(0, False, 0x0187)
        if not desktop:
            raise RuntimeError(
                "Guest input desktop unavailable: "
                + str(ctypes.WinError(ctypes.get_last_error()))
            )
        try:
            name = self.object_name(desktop)
            if name.casefold() != "default":
                raise RuntimeError(
                    "Secure or non-default guest desktop refused: " + name
                )
            if not self.u.SetThreadDesktop(desktop):
                raise ctypes.WinError(ctypes.get_last_error())
        except Exception:
            self.u.CloseDesktop(desktop)
            raise
        old = self.desktop_handle
        self.desktop_handle = desktop
        if old:
            self.u.CloseDesktop(old)
        self.context["input_desktop"] = name

    def state(self):
        title = ctypes.create_unicode_buffer(1024)
        self.u.GetWindowTextW(self.u.GetForegroundWindow(), title, len(title))
        point = w.POINT()
        self.u.GetCursorPos(ctypes.byref(point))
        return {
            "width": self.u.GetSystemMetrics(0),
            "height": self.u.GetSystemMetrics(1),
            "context": self.context,
            "cursor": {"x": point.x, "y": point.y},
            "active_title": title.value,
            "guest_name": socket.gethostname(),
        }

    def desktop_diagnostics(self):
        # Read-only Windows session diagnostics, without attempting input.
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.ProcessIdToSessionId.argtypes = [w.DWORD, ctypes.POINTER(w.DWORD)]
        pid = kernel.GetCurrentProcessId()
        session_id = w.DWORD()
        current_ok = bool(kernel.ProcessIdToSessionId(pid, ctypes.byref(session_id)))
        self.u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
        self.u.GetWindowThreadProcessId.restype = w.DWORD
        self.u.GetThreadDesktop.argtypes = [w.DWORD]
        self.u.GetThreadDesktop.restype = w.HANDLE
        hwnd = self.u.GetForegroundWindow()
        target_pid = w.DWORD()
        foreground_thread = (
            self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(target_pid))
            if hwnd
            else 0
        )
        foreground_session = w.DWORD()
        target_ok = bool(
            target_pid.value
            and kernel.ProcessIdToSessionId(
                target_pid, ctypes.byref(foreground_session)
            )
        )
        self.u.GetProcessWindowStation.argtypes = []
        self.u.GetProcessWindowStation.restype = w.HANDLE
        station = self.u.GetProcessWindowStation()
        own_thread = kernel.GetCurrentThreadId()
        desktop = self.u.GetThreadDesktop(own_thread)
        return {
            "pid": pid,
            "process_session": session_id.value if current_ok else None,
            "foreground_hwnd_present": bool(hwnd),
            "foreground_pid": target_pid.value,
            "foreground_session": foreground_session.value if target_ok else None,
            "foreground_thread_present": bool(foreground_thread),
            "own_station": self.object_name(station) if station else None,
            "own_desktop": self.object_name(desktop) if desktop else None,
            "guest_identity": "wdagutilityaccount",
        }

    def send(self, events):
        if not events:
            return
        values = (InputEvent * len(events))(*events)
        if self.u.SendInput(len(events), values, ctypes.sizeof(InputEvent)) != len(
            events
        ):
            raise RuntimeError(
                "Guest SendInput blocked; error="
                + str(ctypes.get_last_error())
                + "; no host fallback"
            )

    def key(self, vk=0, scan=0, flags=0):
        if vk in (
            0x21,
            0x22,
            0x23,
            0x24,
            0x25,
            0x26,
            0x27,
            0x28,
            0x2D,
            0x2E,
            0x5B,
            0x5C,
        ):
            flags |= 1
        event = InputEvent()
        event.type = 1
        event.data.ki = KeyInput(vk, scan, flags, 0, 0)
        return event

    def move(self, x, y):
        state = self.state()
        if not (0 <= x < state["width"] and 0 <= y < state["height"]):
            raise ValueError("Point outside guest screen")
        if not self.u.SetCursorPos(x, y):
            raise RuntimeError(
                "Guest SetCursorPos blocked; error=" + str(ctypes.get_last_error())
            )
        self.ai_cursor("move")

    def pad_inprocess(self, mode, x=0, y=0, text=""):
        """Scope-limited app automation on the Sandbox Pad's own WinForms UI thread.

        This does not simulate hardware input or control arbitrary apps.
        """
        if mode not in ("type", "click"):
            raise ValueError("Pad automation only supports text or Confirm")
        if not self.u.FindWindowW(None, "UDB Sandbox Pad - guest only"):
            raise RuntimeError(
                "Demo-only app-local input denied: UDB Sandbox Pad not found"
            )
        rid = uuid.uuid4().hex
        request = OUTPUT / ("padreq-" + rid + ".json")
        acknowledgement = OUTPUT / ("padack-" + rid + ".json")
        payload = {
            "id": rid,
            "session_id": self.session_id,
            "mode": mode,
            "x": x,
            "y": y,
            "text": text,
        }
        write_json(request, payload)
        try:
            until = time.monotonic() + 8
            while time.monotonic() < until:
                if acknowledgement.is_file():
                    result = json.loads(acknowledgement.read_text("utf-8-sig"))
                    if (
                        result.get("id") != rid
                        or result.get("session_id") != self.session_id
                    ):
                        raise RuntimeError("Pad acknowledgement identity mismatch")
                    if not result.get("ok"):
                        raise RuntimeError(
                            "Pad rejected action: " + str(result.get("result"))[:240]
                        )
                    return str(result.get("result", ""))[:80]
                time.sleep(0.08)
            raise RuntimeError("Guest Pad did not acknowledge input")
        finally:
            request.unlink(missing_ok=True)
            acknowledgement.unlink(missing_ok=True)

    def uia_demo(self, mode, x=0, y=0, text=""):
        # Guest-only .NET UI Automation: script refuses non-demo windows.
        if mode not in ("click", "type"):
            raise RuntimeError("Unsupported guest UI Automation action")
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        p = subprocess.run(
            [
                _system_executable("powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INPUT / "uia.ps1"),
                "-Mode",
                mode,
                "-X",
                str(x),
                "-Y",
                str(y),
                "-TextB64",
                b64,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=25,
        )
        if p.returncode:
            # Win32 and .NET UIA can be inaccessible from helper PowerShell under
            # ProtectedClient. For *the demo only*, use its app-local event loop.
            return self.pad_inprocess(mode, x, y, text)
        return p.stdout.decode("utf-8", "replace").strip()[-150:]

    def _close_uia(self):
        """Stop only this session-owned PowerShell UIA process."""
        proc = getattr(self, "_uia_process", None)
        self._uia_process = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                if proc.stdin:
                    proc.stdin.close()
                proc.wait(timeout=1.5)
        except (OSError, subprocess.TimeoutExpired):
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def _start_uia(self):
        """One bounded STA worker retained between commands; no host fallback."""
        previous = getattr(self, "_uia_process", None)
        if previous is not None and previous.poll() is None:
            return
        self._close_uia()
        argv = [
            _system_executable("powershell.exe"),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-STA",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INPUT / "accessibility.ps1"),
            "-Persistent",
        ]
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._uia_process = proc
        self._uia_queue = queue.Queue(maxsize=32)
        self._uia_noise = []
        responses = self._uia_queue
        noise = self._uia_noise

        def reader():
            try:
                for raw in iter(lambda: proc.stdout.readline(131074), ""):
                    line = raw.strip()
                    if len(line) > 131072:
                        line = "ERR:UIA reply exceeds 128 KiB"
                    if line == "READY" or line.startswith(("RES:", "ERR:")):
                        try:
                            responses.put_nowait(line)
                        except queue.Full:
                            pass
                    elif line:
                        noise.append(line[:300])
                        if len(noise) > 12:
                            noise.pop(0)
            finally:
                try:
                    responses.put_nowait("EOF")
                except queue.Full:
                    pass

        threading.Thread(target=reader, daemon=True, name="UDBGuestUIA").start()
        try:
            ready = self._uia_queue.get(timeout=12)
            if ready != "READY":
                raise RuntimeError(
                    "UIA worker startup failed: "
                    + str(ready)
                    + "; "
                    + str(self._uia_noise[-2:])[:240]
                )
        except (queue.Empty, RuntimeError):
            self._close_uia()
            raise RuntimeError(
                "Guest UIA helper failed readiness: "
                + str(getattr(self, "_uia_noise", [])[-2:])[:240]
            ) from None

    def accessibility(
        self,
        mode,
        name="",
        automation_id="",
        control_type="",
        value="",
        timeout_seconds=6,
        window_title="",
        window_id="",
    ):
        """Reuse guest UIA process, including its cached active-window element."""
        if mode != "snapshot" and not window_title and not window_id:
            window_title = getattr(self, "uia_window_title", "")
        request = {
            "mode": mode,
            "name": name,
            "automation_id": automation_id,
            "control_type": control_type,
            "value": value,
            "window_title": window_title,
            "timeout_seconds": timeout_seconds,
        }
        if window_id:
            handle, title = self.resolve_window(window_id, "")
            request["window_handle"] = handle
            request["window_title"] = title
        encoded = base64.b64encode(
            json.dumps(request, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        self._start_uia()
        try:
            self._uia_process.stdin.write(encoded + "\n")
            self._uia_process.stdin.flush()
            reply = self._uia_queue.get(timeout=min(25, timeout_seconds + 13))
        except (BrokenPipeError, OSError, queue.Empty):
            self._close_uia()
            raise RuntimeError(
                "Guest UIA helper not responding; no host fallback"
            ) from None
        if reply.startswith("ERR:"):
            raise RuntimeError("Guest UIA rejected request: " + reply[4:][:400])
        if not reply.startswith("RES:"):
            self._close_uia()
            raise RuntimeError("Guest UIA lost its command stream")
        data = json.loads(reply[4:])
        if not isinstance(data, dict) or not data.get("ok"):
            raise RuntimeError("Guest UIA returned invalid response")
        if mode == "snapshot":
            self.uia_window_title = str(data.get("active_window", ""))[:160]
        return data

    def ai_cursor(self, action):
        pos = w.POINT()
        if self.u.GetCursorPos(ctypes.byref(pos)):
            write_json(
                OUTPUT / "cursor.json",
                {"x": int(pos.x), "y": int(pos.y), "action": action},
            )

    def click(self, x, y, button, clicks):
        # The demo app can invoke its own Confirm handler safely even when guest
        # Win32 pointer injection loses access after RDP focus changes.
        if (
            button == "left"
            and clicks == 1
            and self.u.FindWindowW(None, "UDB Sandbox Pad - guest only")
        ):
            self.pad_inprocess("click", x=x, y=y)
            write_json(OUTPUT / "cursor.json", {"x": x, "y": y, "action": "click"})
            return
        self.move(x, y)
        down, up = (0x0002, 0x0004) if button == "left" else (0x0008, 0x0010)
        for _ in range(clicks):
            events = []
            for flags in (down, up):
                event = InputEvent()
                event.type = 0
                event.data.mi = MouseInput(0, 0, 0, flags, 0, 0)
                events.append(event)
            try:
                self.send(events)
            except RuntimeError as exc:
                if "Guest SendInput blocked" not in str(exc) or button != "left":
                    raise
                self.uia_demo("click", x, y)
            if clicks == 2:
                time.sleep(0.07)
        self.ai_cursor("click")

    def type_text(self, text):
        # App-local text input is deterministic and only works on Sandbox Pad.
        if self.u.FindWindowW(None, "UDB Sandbox Pad - guest only"):
            self.pad_inprocess("type", text=text)
            self.ai_cursor("type")
            return
        data = text.encode("utf-16-le")
        events = []
        try:
            for i in range(0, len(data), 2):
                unit = int.from_bytes(data[i : i + 2], "little")
                events.extend(
                    (self.key(scan=unit, flags=4), self.key(scan=unit, flags=6))
                )
                if len(events) >= 256:
                    self.send(events)
                    events = []
            self.send(events)
        except RuntimeError as exc:
            if "Guest SendInput blocked" not in str(exc):
                raise
            self.uia_demo("type", text=text)
        self.ai_cursor("type")

    def press(self, keys):
        names = {
            "ctrl": 0x11,
            "alt": 0x12,
            "shift": 0x10,
            "win": 0x5B,
            "enter": 0x0D,
            "tab": 9,
            "escape": 0x1B,
            "esc": 0x1B,
            "backspace": 8,
            "delete": 0x2E,
            "space": 0x20,
            "left": 0x25,
            "up": 0x26,
            "right": 0x27,
            "down": 0x28,
            "home": 0x24,
            "end": 0x23,
            "pageup": 0x21,
            "pagedown": 0x22,
        }
        names.update({chr(i).lower(): i for i in range(65, 91)})
        names.update({str(i): 48 + i for i in range(10)})
        names.update({f"f{i}": 0x6F + i for i in range(1, 13)})
        parts = keys.lower().split("+")
        if len(parts) > 5 or any(p not in names for p in parts):
            raise ValueError("Unsupported guest key combination")
        if set(parts) in ({"win", "l"}, {"ctrl", "alt", "delete"}):
            raise ValueError("Secure/lock combinations are blocked")
        pressed = []
        try:
            for part in parts:
                self.send([self.key(names[part])])
                pressed.append(names[part])
        finally:
            for vk in reversed(pressed):
                self.send([self.key(vk, flags=2)])

    def scroll(self, delta):
        event = InputEvent()
        event.type = 0
        event.data.mi = MouseInput(0, 0, delta & 0xFFFFFFFF, 0x0800, 0, 0)
        self.send([event])

    def screenshot_dotnet(self, path, state):
        """Guest-only fallback for GDI screen capture blocked by ProtectedClient."""
        import base64

        capture = state.get(
            "capture",
            {
                "x": 0,
                "y": 0,
                "width": state["width"],
                "height": state["height"],
                "output_width": state["width"],
                "output_height": state["height"],
            },
        )
        width, height = capture["output_width"], capture["output_height"]
        script = (
            """$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Drawing
$source=New-Object System.Drawing.Bitmap(CW,CH,[System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
$sourceGraphics=[System.Drawing.Graphics]::FromImage($source)
$bitmap=New-Object System.Drawing.Bitmap(WIDTH,HEIGHT,[System.Drawing.Imaging.PixelFormat]::Format24bppRgb)
$graphics=[System.Drawing.Graphics]::FromImage($bitmap)
try {
  $sourceGraphics.CopyFromScreen(SX,SY,0,0,[System.Drawing.Size]::new(CW,CH))
  $graphics.DrawImage($source,0,0,WIDTH,HEIGHT)
  $bitmap.Save('OUTPUT',[System.Drawing.Imaging.ImageFormat]::Png)
} finally { $graphics.Dispose(); $bitmap.Dispose();$sourceGraphics.Dispose();$source.Dispose() }
""".replace("WIDTH", str(width))
            .replace("HEIGHT", str(height))
            .replace("OUTPUT", str(path))
            .replace("CW", str(capture["width"]))
            .replace("CH", str(capture["height"]))
            .replace("SX", str(capture["x"]))
            .replace("SY", str(capture["y"]))
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        run = subprocess.run(
            [
                _system_executable("powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-EncodedCommand",
                encoded,
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=25,
        )
        if run.returncode or not path.is_file():
            raise RuntimeError(
                "Guest screenshot denied by Windows display security, including .NET fallback"
            )
        return state

    def screenshot(self, path, x=0, y=0, width=0, height=0, max_width=0):
        state = self.state()
        screen_width, screen_height = state["width"], state["height"]
        width = width or screen_width
        height = height or screen_height
        if not (
            0 <= x < screen_width
            and 0 <= y < screen_height
            and 1 <= width <= screen_width - x
            and 1 <= height <= screen_height - y
        ):
            raise ValueError("Capture region outside guest screen")
        capture_width, capture_height = width, height
        if max_width and width > max_width:
            height = max(1, round(height * max_width / width))
            width = max_width
        state["capture"] = {
            "x": x,
            "y": y,
            "width": capture_width,
            "height": capture_height,
            "output_width": width,
            "output_height": height,
            "scale_x": capture_width / width,
            "scale_y": capture_height / height,
        }
        if not 1 <= width <= 4096 or not 1 <= height <= 2160:
            raise ValueError("Guest screen exceeds capture limit")
        dc = self.u.GetDC(None)
        memory = self.g.CreateCompatibleDC(dc)
        bitmap = self.g.CreateCompatibleBitmap(dc, width, height)
        previous = self.g.SelectObject(memory, bitmap)
        try:
            self.g.StretchBlt.argtypes = [
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
            if not self.g.StretchBlt(
                memory,
                0,
                0,
                width,
                height,
                dc,
                x,
                y,
                capture_width,
                capture_height,
                0x00CC0020 | 0x40000000,
            ):
                return self.screenshot_dotnet(path, state)
            self.g.SelectObject(memory, previous)
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
            raw = ctypes.create_string_buffer(width * height * 4)
            if (
                self.g.GetDIBits(dc, bitmap, 0, height, raw, ctypes.byref(header), 0)
                != height
            ):
                raise ctypes.WinError()
            data = raw.raw
            rgb = bytearray(width * height * 3)
            rgb[0::3] = data[2::4]
            rgb[1::3] = data[1::4]
            rgb[2::3] = data[0::4]
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
            path.write_bytes(png)
            return state
        finally:
            self.g.SelectObject(memory, previous)
            self.g.DeleteObject(bitmap)
            self.g.DeleteDC(memory)
            self.u.ReleaseDC(None, dc)


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, path)


def read_command_mailbox(path):
    """Reject incomplete/torn writes; hash checks integrity, not authorization."""
    try:
        raw = path.read_bytes()
        if not 3 <= len(raw) <= 16000:
            return None
        envelope = json.loads(raw.decode("utf-8"))
        if not isinstance(envelope, dict) or set(envelope) != {"payload", "sha256"}:
            return None
        payload = envelope["payload"]
        if not isinstance(payload, dict) or not isinstance(envelope["sha256"], str):
            return None
        data = json.dumps(
            payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if not hmac.compare_digest(
            hashlib.sha256(data).hexdigest(), envelope["sha256"]
        ):
            return None
        rid = payload.get("request_id")
        if not isinstance(rid, str) or not re.fullmatch("[a-f0-9]{32}", rid):
            return None
        return payload
    except (OSError, ValueError, TypeError, UnicodeError):
        return None


def main():
    config = json.loads((INPUT / "session.json").read_text("utf-8"))
    desktop = GuestDesktop(config)
    expires = min(float(config["expires"]), time.time() + 1800)
    OUTPUT.mkdir(exist_ok=True)
    (OUTPUT / "replies").mkdir(exist_ok=True)
    initial = desktop.state()
    initial["guest_diagnostics"] = desktop.desktop_diagnostics()
    processed = set()
    write_json(
        OUTPUT / "ready.json",
        {
            "session_id": config["session_id"],
            "state": initial,
            "mailbox_protocol": 1,
            "capabilities": ["tasks_v1"],
        },
    )
    overlay = None
    try:
        overlay = subprocess.Popen(
            [
                _system_executable("powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(INPUT / "overlay.ps1"),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        write_json(OUTPUT / "overlay-launch-error.json", {"error": str(exc)[:300]})
    demo = None
    last_activity = 0.0
    try:
        while time.time() < expires:
            pending = []
            mailbox_path = INPUT / "command-mailbox.json"
            mailbox = read_command_mailbox(mailbox_path)
            if mailbox is not None:
                pending.append((mailbox_path, mailbox))
            pending.extend(
                (p, None) for p in sorted((INPUT / "commands").glob("*.json"))
            )
            for path, mailbox_payload in pending:
                rid = (
                    mailbox_payload["request_id"]
                    if mailbox_payload is not None
                    else path.stem
                )
                if rid in processed or not re.fullmatch("[a-f0-9]{32}", rid):
                    continue
                processed.add(rid)
                try:
                    if mailbox_payload is None:
                        if path.stat().st_size > 16000:
                            raise ValueError("Command too large")
                        c = json.loads(path.read_text("utf-8"))
                    else:
                        c = mailbox_payload
                    if (
                        c["session_id"] != config["session_id"]
                        or time.time() > c["expires"]
                    ):
                        raise ValueError("Wrong or expired command")
                    action = c["action"]
                    started = time.perf_counter()
                    last_activity = time.monotonic()
                    desktop.refresh_input_desktop()
                    print(
                        json.dumps(
                            {
                                "event": "command_start",
                                "id": rid,
                                "action": action,
                                "at": time.time(),
                            }
                        ),
                        flush=True,
                    )
                    if action == "batch":
                        result = desktop.batch(c)
                    elif action == "app":
                        result = desktop.app(c)
                    elif action in (
                        "snapshot",
                        "wait_for",
                        "element_invoke",
                        "element_set_text",
                    ):
                        result = desktop.accessibility(
                            action,
                            name=c.get("name", ""),
                            automation_id=c.get("automation_id", ""),
                            control_type=c.get("control_type", ""),
                            value=c.get("value", ""),
                            timeout_seconds=int(c.get("timeout_seconds", 6)),
                            window_title=c.get("window_title", ""),
                            window_id=c.get("window_id", ""),
                        )
                    elif action == "screenshot":
                        result = desktop.screenshot(
                            OUTPUT / "replies" / (rid + ".png"),
                            **{
                                k: c.get(k, 0)
                                for k in ("x", "y", "width", "height", "max_width")
                            },
                        )
                    elif action == "state":
                        result = desktop.state()
                    elif action == "move":
                        desktop.move(int(c["x"]), int(c["y"]))
                        result = desktop.state()
                    elif action == "click":
                        desktop.click(
                            int(c["x"]), int(c["y"]), c["button"], int(c["clicks"])
                        )
                        result = desktop.state()
                    elif action == "type":
                        if len(c["text"]) > 2000:
                            raise ValueError("Text too long")
                        desktop.type_text(c["text"])
                        result = desktop.state()
                    elif action == "press":
                        desktop.press(c["keys"])
                        result = desktop.state()
                    elif action == "scroll":
                        delta = int(c["delta"])
                        if not -1200 <= delta <= 1200:
                            raise ValueError("Scroll limit")
                        desktop.scroll(delta)
                        result = desktop.state()
                    elif action == "open_demo":
                        if demo is None or demo.poll() is not None:
                            demo = subprocess.Popen(
                                [
                                    _system_executable("powershell.exe"),
                                    "-NoLogo",
                                    "-NoProfile",
                                    "-ExecutionPolicy",
                                    "Bypass",
                                    "-File",
                                    str(INPUT / "demo.ps1"),
                                ]
                            )
                        time.sleep(0.8)
                        handle = desktop.u.FindWindowW(
                            None, "UDB Sandbox Pad - guest only"
                        )
                        if handle:
                            desktop.u.ShowWindow(handle, 9)
                            desktop.u.SetForegroundWindow(handle)
                        result = desktop.state()
                    elif action == "close":
                        write_json(
                            OUTPUT / "replies" / (rid + ".json"),
                            {"ok": True, "closing": True},
                        )
                        return
                    else:
                        raise ValueError("Unknown guest action")
                    write_json(
                        OUTPUT / "replies" / (rid + ".json"),
                        {
                            "ok": True,
                            "state": result,
                            "session_id": config["session_id"],
                            "request_id": rid,
                            "timings": {
                                "guest_action_ms": round(
                                    (time.perf_counter() - started) * 1000, 3
                                )
                            },
                        },
                    )
                    print(
                        json.dumps(
                            {
                                "event": "command_done",
                                "id": rid,
                                "action": action,
                                "at": time.time(),
                            }
                        ),
                        flush=True,
                    )
                except Exception as error:
                    write_json(
                        OUTPUT / "replies" / (rid + ".json"),
                        {"ok": False, "error": str(error)[:500]},
                    )
            time.sleep(0.02 if time.monotonic() - last_activity < 2 else 0.1)
    finally:
        desktop._close_uia()
        if overlay and overlay.poll() is None:
            overlay.terminate()
        # This shutdown is reachable only after GuestDesktop's OS identity guard.
        subprocess.Popen([_system_executable("shutdown.exe"), "/s", "/t", "0"])


# Bounded task execution. All entry points remain behind assert_guest().
def validate_steps(steps):
    if not isinstance(steps, list) or not 1 <= len(steps) <= 12:
        raise ValueError("Batch requires 1..12 steps")
    allowed = {
        "state",
        "snapshot",
        "wait_for",
        "element_invoke",
        "element_set_text",
        "move",
        "click",
        "type",
        "press",
        "scroll",
        "app",
    }
    common = {
        "action",
        "x",
        "y",
        "text",
        "keys",
        "button",
        "clicks",
        "delta",
        "name",
        "automation_id",
        "control_type",
        "value",
        "timeout_seconds",
        "window_title",
        "operation",
        "app",
        "path",
        "width",
        "height",
        "window_id",
    }
    result = []
    for step in steps:
        if (
            not isinstance(step, dict)
            or set(step) - common
            or step.get("action") not in allowed
        ):
            raise ValueError("Invalid batch step or unknown fields")
        if len(json.dumps(step, ensure_ascii=False).encode("utf8")) > 5000:
            raise ValueError("Batch step too large")
        if step["action"] == "app" and step.get("operation") == "close":
            raise ValueError("Closing applications requires a separate explicit action")
        result.append(dict(step))
    if len(json.dumps(result, ensure_ascii=False).encode("utf8")) > 12000:
        raise ValueError("Batch payload exceeds 12000 bytes")
    return result


def _windows(self):
    rows = []
    refs = getattr(self, "_window_refs", {})
    observed = {}
    known = {(v[0], v[1], v[2]): k for k, v in refs.items()}
    self.u.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    self.u.IsWindowVisible.argtypes = [w.HWND]
    self.u.IsWindowVisible.restype = w.BOOL
    self.u.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    self.u.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]

    def visit(hwnd, _):
        if self.u.IsWindowVisible(hwnd):
            title = ctypes.create_unicode_buffer(512)
            self.u.GetWindowTextW(hwnd, title, len(title))
            if title.value:
                pid = w.DWORD()
                rect = w.RECT()
                self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                self.u.GetWindowRect(hwnd, ctypes.byref(rect))
                cls = ctypes.create_unicode_buffer(256)
                self.u.GetClassNameW(hwnd, cls, len(cls))
                identity = (int(hwnd), pid.value, cls.value)
                token = known.get(identity) or uuid.uuid4().hex
                observed[token] = identity
                rows.append(
                    {
                        "window_id": token,
                        "title": title.value[:160],
                        "pid": pid.value,
                        "class_name": cls.value,
                        "bounds": [
                            rect.left,
                            rect.top,
                            rect.right - rect.left,
                            rect.bottom - rect.top,
                        ],
                    }
                )
        return len(rows) < 100

    self.u.EnumWindows.argtypes = [callback_type, w.LPARAM]
    if not self.u.EnumWindows(callback_type(visit), 0) and len(rows) < 100:
        raise ctypes.WinError(ctypes.get_last_error())
    self._window_refs = observed
    return {"windows": rows, "truncated": len(rows) >= 100, "guest_only": True}


def _resolve_window(self, window_id="", title=""):
    rows = self.windows()["windows"]
    if window_id:
        if not isinstance(window_id, str) or not re.fullmatch(
            "[a-f0-9]{32}", window_id
        ):
            raise ValueError("Invalid observed window ID")
        matches = [r for r in rows if r["window_id"] == window_id]
    else:
        if not isinstance(title, str) or not title or len(title) > 160:
            raise ValueError("Observed window_id or exact window_title required")
        matches = [r for r in rows if r["title"] == title]
    if len(matches) != 1:
        raise ValueError("Guest window missing, stale or ambiguous; list windows again")
    token = matches[0]["window_id"]
    hwnd, pid, cls = self._window_refs[token]
    current = w.DWORD()
    self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(current))
    if current.value != pid:
        raise ValueError("Guest window process changed")
    return hwnd, matches[0]["title"]


def _app(self, c):
    operation = c.get("operation", "list")
    owned = getattr(self, "_apps", None)
    if owned is None:
        self._apps = {}
        owned = self._apps
    if operation == "list":
        result = self.windows()
        system = Path(os.environ.get("WINDIR", r"C:\Windows"))
        result["available_apps"] = {
            name: [str(p) for p in paths if p.is_file()]
            for name, paths in {
                "notepad": [
                    system / "System32" / "notepad.exe",
                    system / "notepad.exe",
                ],
                "explorer": [system / "explorer.exe"],
            }.items()
        }
        return result
    if operation == "open":
        name = c.get("app", "")
        apps = {
            "notepad": str(
                Path(os.environ.get("WINDIR", r"C:\Windows"))
                / "System32"
                / "notepad.exe"
            ),
            "explorer": str(
                Path(os.environ.get("WINDIR", r"C:\Windows")) / "explorer.exe"
            ),
        }
        if name not in apps:
            raise ValueError("Guest app must be notepad or explorer")
        if name == "notepad" and not Path(apps[name]).is_file():
            apps[name] = str(
                Path(os.environ.get("WINDIR", r"C:\Windows")) / "notepad.exe"
            )
        if not Path(apps[name]).is_file():
            raise ValueError("Guest application is not installed: " + name)
        relative = c.get("path", "")
        target = Path(r"C:\Work")
        if relative:
            if (
                not isinstance(relative, str)
                or len(relative) > 200
                or ":" in relative
                or "\0" in relative
            ):
                raise ValueError("Invalid guest relative path")
            parts = relative.replace("\\", "/").split("/")
            if any(p in ("", "..") or p.endswith((" ", ".")) for p in parts):
                raise ValueError("Unsafe guest relative path")
            target = target.joinpath(*parts).resolve()
            if not target.is_relative_to(Path(r"C:\Work")):
                raise ValueError("Guest path escaped Work")
            if not target.exists():
                raise ValueError("Guest app target must already exist")
        args = [apps[name]]
        if name == "explorer" or relative:
            args.append(str(target))
        proc = subprocess.Popen(args, shell=False)
        owned[proc.pid] = proc
        return {
            "launched": True,
            "pid": proc.pid,
            "app": name,
            "verified": False,
            "guest_only": True,
        }
    hwnd, title = self.resolve_window(c.get("window_id", ""), c.get("window_title", ""))
    pid = w.DWORD()
    self.u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if operation == "focus":
        self.u.ShowWindow(hwnd, 9)
        self.u.SetForegroundWindow(hwnd)
        if self.u.GetForegroundWindow() != hwnd:
            self.accessibility(
                "window_focus", window_id=c.get("window_id", ""), window_title=title
            )
            until = time.monotonic() + 0.5
            while self.u.GetForegroundWindow() != hwnd and time.monotonic() < until:
                time.sleep(0.02)
        if self.u.GetForegroundWindow() != hwnd:
            raise RuntimeError("Windows denied foreground activation")
        return {"operation": "focus", "title": title, "verified": True}
    if operation == "resize":
        width, height = c.get("width"), c.get("height")
        x, y = c.get("x", 0), c.get("y", 0)
        state = self.state()
        if any(type(v) is not int for v in (x, y, width, height)) or not (
            0 <= x
            and 0 <= y
            and 200 <= width <= state["width"] - x
            and 100 <= height <= state["height"] - y
        ):
            raise ValueError("Guest window rectangle outside screen")
        self.u.MoveWindow.argtypes = [
            w.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            w.BOOL,
        ]
        if not self.u.MoveWindow(hwnd, x, y, width, height, True):
            raise ctypes.WinError(ctypes.get_last_error())
        rect = w.RECT()
        self.u.GetWindowRect(hwnd, ctypes.byref(rect))
        bounds = [rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top]
        return {
            "operation": "resize",
            "bounds": bounds,
            "verified": bounds == [x, y, width, height],
        }
    if operation == "close":
        proc = owned.get(pid.value)
        if proc is None or proc.poll() is not None:
            raise ValueError(
                "Only a live application launched by this session can be closed"
            )
        self.u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        if not self.u.PostMessageW(hwnd, 0x0010, 0, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        return {"operation": "close", "requested": True, "verified": False}
    raise ValueError("Unknown guest application operation")


def _step(self, c):
    action = c["action"]
    if action == "state":
        return self.state()
    if action == "app":
        return self.app(c)
    if action in ("snapshot", "wait_for", "element_invoke", "element_set_text"):
        return self.accessibility(
            action,
            **{
                k: c[k]
                for k in (
                    "name",
                    "automation_id",
                    "control_type",
                    "value",
                    "timeout_seconds",
                    "window_title",
                    "window_id",
                )
                if k in c
            },
        )
    if action == "move":
        self.move(c.get("x", 0), c.get("y", 0))
    elif action == "click":
        self.click(
            c.get("x", 0), c.get("y", 0), c.get("button", "left"), c.get("clicks", 1)
        )
    elif action == "type":
        self.type_text(c.get("text", ""))
    elif action == "press":
        self.press(c.get("keys", ""))
    elif action == "scroll":
        self.scroll(c.get("delta", 0))
    else:
        raise ValueError("Unsupported guest step")
    return self.state()


def _batch(self, c):
    steps = validate_steps(c["steps"])
    until = min(c["expires"], time.time() + 30)
    results = []
    for index, step in enumerate(steps):
        if time.time() >= until:
            raise TimeoutError("Batch deadline reached; completed=" + str(len(results)))
        cancel = INPUT / "cancel-mailbox.json"
        try:
            if (
                json.loads(cancel.read_text("utf8")).get("session_id")
                == self.session_id
            ):
                raise RuntimeError("Batch cancelled between steps")
        except (OSError, ValueError):
            pass
        started = time.perf_counter()
        try:
            result = self.step(step)
            results.append(
                {
                    "index": index,
                    "ok": True,
                    "result": result,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                }
            )
            write_json(
                OUTPUT / "progress.json",
                {
                    "session_id": self.session_id,
                    "request_id": c["request_id"],
                    "completed": index + 1,
                    "total": len(steps),
                },
            )
        except Exception as error:
            return {
                "completed": len(results),
                "total": len(steps),
                "failed_index": index,
                "ok": False,
                "error": str(error)[:300],
                "steps": results,
                "rollback": "No automatic rollback for UI side effects",
            }
    return {
        "completed": len(results),
        "total": len(steps),
        "ok": True,
        "steps": results,
    }


GuestDesktop.windows = _windows
GuestDesktop.resolve_window = _resolve_window
GuestDesktop.app = _app
GuestDesktop.step = _step
GuestDesktop.batch = _batch

if __name__ == "__main__":
    main()
