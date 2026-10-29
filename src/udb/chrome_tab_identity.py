"""Chrome tab identity for native AI overlays, without extension or CDP.

Only UI Automation runtime IDs, tab geometry and selection are observed.
No tab names, URLs, cookies, DOM or browser profile are read or persisted.
A tab is registered only when a new UIA TabItem appears after UDB opens it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path


def marker_path() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    return (
        Path(base) / "UniversalDesktopBridge" / "ai-chrome-tabs.json" if base else None
    )


def observe_tabs(hwnd: int) -> list[dict] | None:
    """Read bounded Chrome tab UIA identities; None means not verifiable."""
    if os.name != "nt" or type(hwnd) is not int or hwnd <= 0:
        return None
    script = Path(__file__).with_name("udb_chrome_tab_uia.ps1")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / (
        "System32/WindowsPowerShell/v1.0/powershell.exe"
    )
    try:
        completed = subprocess.run(
            [
                str(powershell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Hwnd",
                str(hwnd),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=7,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        if completed.returncode or len(completed.stdout) > 65536:
            return None
        data = json.loads(completed.stdout.decode("utf-8-sig"))
        if (
            not isinstance(data, dict)
            or data.get("ok") is not True
            or not isinstance(data.get("tabs"), list)
        ):
            return None
        rows = []
        for tab in data["tabs"][:100]:
            if (
                isinstance(tab, dict)
                and isinstance(tab.get("id"), str)
                and 1 <= len(tab["id"]) <= 150
                and isinstance(tab.get("selected"), bool)
                and isinstance(tab.get("bounds"), list)
                and len(tab["bounds"]) == 4
                and all(type(v) is int for v in tab["bounds"])
            ):
                rows.append(tab)
        return rows
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        return None


def register_new_tab(hwnd: int, pid: int, previous: list[dict] | None) -> bool:
    """Register only a newly observed Chrome tab, never an existing user tab."""
    path = marker_path()
    if path is None or previous is None or not previous:
        # Empty previous list can be legitimate when Chrome has zero tabs;
        # UIA snapshots from a running Chrome window must have >=1.
        return False
    seen = {row["id"] for row in previous}
    deadline = time.monotonic() + 5
    candidate = None
    while time.monotonic() < deadline:
        current = observe_tabs(hwnd)
        if current is not None:
            fresh = [row for row in current if row["id"] not in seen]
            if len(fresh) == 1:
                candidate = fresh[0]
                break
            if len(fresh) > 1:
                return False  # ambiguous; never claim a user-created tab
        time.sleep(0.25)
    if candidate is None:
        return False
    try:
        records = []
        if path.is_file() and path.stat().st_size < 4096:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and isinstance(existing.get("tabs"), list):
                records = [
                    row
                    for row in existing["tabs"]
                    if isinstance(row, dict)
                    and type(row.get("hwnd")) is int
                    and type(row.get("pid")) is int
                    and isinstance(row.get("id"), str)
                ][-7:]
        records = [
            row
            for row in records
            if not (row["hwnd"] == hwnd and row["id"] == candidate["id"])
        ]
        records.append({"hwnd": hwnd, "pid": pid, "id": candidate["id"]})
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + "." + str(os.getpid()) + ".tmp")
        try:
            temp.write_text(
                json.dumps({"version": 1, "tabs": records}, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return True
    except (OSError, ValueError, TypeError):
        return False
