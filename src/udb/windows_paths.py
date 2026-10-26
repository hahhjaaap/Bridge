"""Resolve a tiny allowlist of Windows system executables without PATH lookup."""

from __future__ import annotations

import os
from pathlib import Path

_ALLOWED = frozenset({"powershell.exe", "taskkill.exe", "shutdown.exe"})


def system_executable(name: str) -> str:
    """Return an absolute path for an allowlisted Windows system executable."""
    if not isinstance(name, str) or name.casefold() not in _ALLOWED:
        raise ValueError("Windows executable is not allowlisted")
    if os.name != "nt":
        raise OSError("Windows system executables are only available on Windows")

    normalized = name.casefold()
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    if normalized == "powershell.exe":
        path = root / "System32" / "WindowsPowerShell" / "v1.0" / normalized
    else:
        path = root / "System32" / normalized
    if not path.is_file():
        raise FileNotFoundError(
            f"Required Windows system executable is missing: {normalized}"
        )
    return str(path)
