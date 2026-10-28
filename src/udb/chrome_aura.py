"""Opt-in Chrome Aura launcher; no Chrome profile inspection or CDP attachment."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

EXTENSION_ID = "ancaoelohcjcbbmncfhimpookhdelhle"
MARKER_NAME = "chrome-aura-enabled"


def marker_path(appdata: str | Path | None = None) -> Path | None:
    base = appdata if appdata is not None else os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    return Path(base) / "UniversalDesktopBridge" / MARKER_NAME


def launch_target(url: str, appdata: str | Path | None = None) -> tuple[str, bool]:
    """Use a local extension launch page only after the owner opts in.

    The URL is encoded in the extension page fragment (never sent to a
    third-party site). No secrets or browsing history are read.
    """
    marker = marker_path(appdata)
    if marker is None:
        return url, False
    try:
        if not marker.is_file() or marker.stat().st_size > 128:
            return url, False
        if marker.read_text(encoding="ascii").strip() != EXTENSION_ID:
            return url, False
    except (OSError, UnicodeError):
        return url, False
    return f"chrome-extension://{EXTENSION_ID}/launch.html#{quote(url, safe='')}", True
