"""Verified bundled helpers for a standalone UDB executable.

Build assets contain only public, explicitly inventoried binaries and notices.
They are separate from owner state, credentials and permission receipts.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

FILENAMES = {
    "tunnel-client": "tunnel-client.exe",
    "ffmpeg": "ffmpeg.exe",
    "ffprobe": "ffprobe.exe",
}
MAX_BINARY = 512 * 1024 * 1024
MAX_MANIFEST = 128 * 1024
MAX_NOTICE = 4 * 1024 * 1024


def _regular(path: Path):
    for item in (path, *path.parents):
        if item.is_symlink() or (
            item.exists() and getattr(item.lstat(), "st_file_attributes", 0) & 0x400
        ):
            raise ValueError("Linked component path refused")
    if not path.is_file() or path.stat().st_nlink != 1:
        raise ValueError("Missing or hardlinked component file")
    return path


def _sha256(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verified_assets(root):
    """Validate an optional build/runtime inventory; never scan unrelated files."""
    root = Path(root).absolute()
    manifest = root / "components.json"
    if not manifest.exists():
        return []
    _regular(manifest)
    with manifest.open("rb") as stream:
        raw = stream.read(MAX_MANIFEST + 1)
    if len(raw) > MAX_MANIFEST:
        raise ValueError("Component inventory exceeds size limit")
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "components"}
        or data["schema"] != 1
        or not isinstance(data["components"], list)
        or len(data["components"]) > len(FILENAMES)
    ):
        raise ValueError("Invalid component inventory")
    rows, names = [], set()
    fields = {"name", "version", "binary", "sha256", "license", "source_url"}
    for record in data["components"]:
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError("Invalid component record")
        if any(not isinstance(record[key], str) for key in fields):
            raise ValueError("Component fields must be text")
        name = record["name"]
        if name not in FILENAMES or name in names:
            raise ValueError("Unknown or duplicate component")
        if (
            record["binary"] != "bin/" + FILENAMES[name]
            or record["license"] != "licenses/" + name + ".txt"
            or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])
            or not 1 <= len(record["version"]) <= 100
            or not record["source_url"].startswith("https://")
            or len(record["source_url"]) > 2048
        ):
            raise ValueError("Invalid component path or provenance")
        url = urlsplit(record["source_url"])
        if (
            not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Component provenance must be a public HTTPS URL without credentials"
            )
        binary = _regular(root / record["binary"])
        notice = _regular(root / record["license"])
        if binary.stat().st_size > MAX_BINARY or notice.stat().st_size > MAX_NOTICE:
            raise ValueError("Component or notice exceeds size limit")
        if _sha256(binary) != record["sha256"]:
            raise ValueError("Bundled component checksum mismatch")
        names.add(name)
        rows.append(dict(record))
    return rows


def bundle_root():
    return Path(__file__).with_name("runtime")


def component_path(name, home=None):
    """Prefer the embedded helper; retain explicit legacy installations."""
    if name not in FILENAMES:
        raise ValueError("Unknown bundled component")
    root = bundle_root()
    for record in verified_assets(root):
        if record["name"] == name:
            return root / record["binary"]
    if home is not None:
        for folder in ("tools", "vendor"):
            candidate = Path(home) / folder / FILENAMES[name]
            if candidate.is_file():
                return _regular(candidate)
    if name in ("ffmpeg", "ffprobe"):
        candidate = shutil.which(name)
        return Path(candidate) if candidate else None
    return None


def runtime_report(probe=False):
    frozen = bool(getattr(sys, "frozen", False))
    records = verified_assets(bundle_root())
    report = {
        "mode": "standalone" if frozen else "source",
        "python_embedded": frozen,
        "external_python_required": not frozen,
        "source_checkout_required": not frozen,
        "desktop_commander_required": False,
        "node_required": False,
        "windows_required": True,
        "chatgpt_connection_requires_network_and_account": True,
        "components": records,
        "office_modules": {
            name: importlib.util.find_spec(name) is not None
            for name in ("openpyxl", "docx", "pypdf", "reportlab")
        },
        "optional_system_features": [
            "Windows Sandbox and its configured guest runtime",
            "An installed supported browser for browser automation",
            "FFmpeg/FFprobe for media download when not bundled",
        ],
    }
    if probe:
        from .launcher import child_environment

        probes = {}
        for record in records:
            path = bundle_root() / record["binary"]
            result = subprocess.run(
                [
                    str(path),
                    "--version" if record["name"] == "tunnel-client" else "-version",
                ],
                env=child_environment(),
                capture_output=True,
                timeout=30,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            probes[record["name"]] = {"ok": result.returncode == 0}
        report["component_probes"] = probes
    return report
