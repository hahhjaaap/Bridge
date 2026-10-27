"""Optional current-user logon startup. No elevation or permission activation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess

from .launcher import command
from .maintenance import installation
from .owner_command import CommandError
from .windows_paths import system_executable

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def identity(home):
    return "UDB-" + hashlib.sha256(str(home).casefold().encode()).hexdigest()[:16]


def startup_command(settings):
    found = installation()
    if found:
        root, record = found
        if record["home"] != str(settings.home):
            raise CommandError("Use the managed launcher's saved home for autostart.")
        argv = [
            system_executable("powershell.exe"),
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(root / "bin/udb.ps1"),
            "start",
            "--detach",
        ]
    else:
        argv = command(["--home", str(settings.home), "start", "--detach"])
    # Encode literal PowerShell arguments; paths cannot inject a command.
    script = (
        "& "
        + " ".join("'" + arg.replace("'", "''") + "'" for arg in argv)
        + "; exit $LASTEXITCODE"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.list2cmdline(
        [
            system_executable("powershell.exe"),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-WindowStyle",
            "Hidden",
            "-EncodedCommand",
            encoded,
        ]
    )


def status(settings, backend=None):
    if os.name != "nt" and backend is None:
        return {"supported": False, "enabled": False, "matches_current": False}
    if backend is None:
        import winreg as backend
    try:
        with backend.OpenKey(backend.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, kind = backend.QueryValueEx(key, identity(settings.home))
        return {
            "supported": True,
            "enabled": True,
            "matches_current": kind == backend.REG_SZ
            and value == startup_command(settings),
        }
    except FileNotFoundError:
        return {"supported": True, "enabled": False, "matches_current": False}


def run(settings, action, *, as_json=False, backend=None):
    if os.name != "nt" and backend is None:
        raise CommandError("Autostart requires Windows.")
    if backend is None:
        import winreg as backend
    if action not in ("enable", "disable", "status"):
        raise CommandError("Choose autostart enable, disable or status.")
    if action == "enable":
        # Enabling startup must not trigger a first-run wizard in a hidden process.
        from .onboarding import saved_connection, saved_mode

        if saved_mode(settings) != "offline" and not saved_connection(settings):
            raise CommandError("Run udb setup before enabling autostart.")
        value = startup_command(settings)
        with backend.CreateKeyEx(
            backend.HKEY_CURRENT_USER, RUN_KEY, 0, backend.KEY_SET_VALUE
        ) as key:
            backend.SetValueEx(key, identity(settings.home), 0, backend.REG_SZ, value)
    elif action == "disable":
        try:
            with backend.OpenKey(
                backend.HKEY_CURRENT_USER, RUN_KEY, 0, backend.KEY_SET_VALUE
            ) as key:
                backend.DeleteValue(key, identity(settings.home))
        except FileNotFoundError:
            pass
    report = status(settings, backend)
    print(
        json.dumps(report)
        if as_json
        else f"UDB logon startup: {'enabled' if report['enabled'] else 'disabled'}. Run udb start to view logs."
    )
    return 0
