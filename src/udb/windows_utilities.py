"""Small Windows conveniences behind the existing computer-access grant."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from .core import PolicyError
from .desktop_control import InputControl
from .windows_actions import UTILITY_ACTIONS
from .windows_paths import system_executable

MAX_CLIPBOARD = 32768


class WindowsBackend:
    def clipboard(self, text=None, expected_version=""):
        import win32clipboard as clip
        import win32con

        # Opening may fail while another application owns the clipboard. Return
        # the error for inspection; never loop or overwrite after a conflict.
        clip.OpenClipboard()
        try:
            version = str(clip.GetClipboardSequenceNumber())
            if text is None:
                if not clip.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return dict(text="", version=version, format="non_text")
                value = clip.GetClipboardData(win32con.CF_UNICODETEXT)
                if len(value) > MAX_CLIPBOARD:
                    raise PolicyError("Clipboard text exceeds 32768 characters")
                return dict(text=value, version=version, format="text")
            if expected_version != version:
                raise PolicyError("Clipboard changed; read it again before writing")
            clip.EmptyClipboard()
            clip.SetClipboardText(text, win32con.CF_UNICODETEXT)
            return dict(written=True, version=str(clip.GetClipboardSequenceNumber()))
        finally:
            clip.CloseClipboard()

    def helper(self, request):
        result = subprocess.run(
            [
                system_executable("powershell.exe"),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(Path(__file__).with_name("windows_utilities.ps1")),
            ],
            input=json.dumps(request, ensure_ascii=True),
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=10,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode or len(result.stdout) > 262144:
            raise PolicyError("Windows utility failed; inspect before retrying")
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or not value.get("ok"):
            raise PolicyError("Windows utility declined the request")
        return value

    def apps(self):
        return self.helper({"action": "apps"})["apps"]

    def launch(self, app_id):
        os.startfile("shell:AppsFolder\\" + app_id)
        return dict(
            requested=True,
            app_id=app_id,
            may_activate_window=True,
            launched_verified=False,
        )

    def notify(self, title, text):
        return self.helper({"action": "notify", "title": title, "text": text})


class Utilities:
    def __init__(self, agent, backend=None, control=None):
        self.agent = agent
        self.backend = backend or WindowsBackend()
        self.control = control

    def dispatch(self, action, text="", title="UDB", name="", expected_version=""):
        if action not in UTILITY_ACTIONS:
            raise PolicyError("Unsupported Windows utility")
        for value, limit in (
            (text, MAX_CLIPBOARD),
            (title, 120),
            (name, 256),
            (expected_version, 32),
        ):
            if not isinstance(value, str) or len(value) > limit or "\0" in value:
                raise PolicyError("Invalid Windows utility text")
        if action in {"clipboard_set", "launch_app"}:
            if self.control is None:
                self.control = InputControl(self.agent)
            self.control.check()
        if action == "clipboard_get":
            return self.backend.clipboard()
        if action == "clipboard_set":
            if not expected_version.isdecimal():
                raise PolicyError(
                    "Read clipboard first and supply its expected_version"
                )
            return self.backend.clipboard(text, expected_version)
        if action == "notify":
            if not title.strip() or not text.strip() or len(text) > 1000:
                raise PolicyError(
                    "Notification needs a title and 1..1000 text characters"
                )
            return self.backend.notify(title, text)
        apps = self.backend.apps()
        if action == "apps":
            return dict(apps=apps, limit=300)
        matches = [
            app
            for app in apps
            if name.casefold() in (app["name"].casefold(), app["app_id"].casefold())
        ]
        if len(matches) != 1:
            raise PolicyError(
                "Choose one exact name or app_id from apps; name is unknown or ambiguous"
            )
        return self.backend.launch(matches[0]["app_id"])
