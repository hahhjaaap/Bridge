"""Strict path and payload allowlists for offline UDB releases.

These checks do not authenticate a publisher; they prevent accidental inclusion
of local state and flag common credential formats before a ZIP is published.
"""
from __future__ import annotations

import io
import re
import zipfile
from pathlib import PurePosixPath

_BLOCKED_PARTS = frozenset({
    "state", "projects", ".git", ".venv", "tools", "__pycache__",
    ".udb-recovery", ".udb-work-state", "tunnel",
})
_BLOCKED_NAMES = frozenset({
    "http-token", "connection.json", "computer-access.dpapi",
    "agent.sqlite3", "host-settings.json",
})
_BLOCKED_EXTENSIONS = (".bak", ".pyc", ".zip")
_ROOT_EXTRA = frozenset({"UniversalDesktopBridge.exe", "RELEASE-MANIFEST.json"})
_RUNTIME_FILES = frozenset({
    "python.exe", "python3.dll", "python311.dll", "vcruntime140.dll",
    "vcruntime140_1.dll", "LICENSE.txt", "python311._pth",
})
_WORKER_SUFFIXES = {
    "udb": frozenset({".py", ".ps1"}),
    "docs": frozenset({".md", ".json", ".png"}),
    "command": frozenset({".py", ".ps1", ".json", ".md"}),
}
_SECRET_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    rb"(?<![A-Za-z0-9_])(?:ghp_|gho_|ghu_|ghs_|ghr_)[A-Za-z0-9]{30,}",
    rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{40,}",
    rb"(?<![A-Za-z0-9_])sk-(?!synthetic-|test-|example-|fake-|demo-)[A-Za-z0-9_-]{24,}",
    rb"(?<![A-Za-z0-9_])AKIA[A-Z0-9]{16}(?![A-Za-z0-9])",
    rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    rb"(?<![A-Za-z0-9_])Bearer[ \t]+[A-Za-z0-9._~+/-]{32,}",
    rb"(?<![A-Za-z0-9_])eyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{15,}",
    rb"""["'](?:http-token|api[_-]?key|access[_-]?token|secret|password)["']\s*:\s*["'][A-Za-z0-9+/=_-]{24,}["']""",
))


def allowed_release_member(name: str, root_files: tuple[str, ...]) -> bool:
    """Require an explicit release layout, without trusting the ZIP manifest."""
    if not name or "\\" in name or ":" in name or "\x00" in name:
        return False
    if any(ord(char) < 32 for char in name):
        return False
    parts = name.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return False
    lower_parts = tuple(part.casefold() for part in parts)
    if any(part in _BLOCKED_PARTS or part in _BLOCKED_NAMES for part in lower_parts):
        return False
    if any(part.endswith(_BLOCKED_EXTENSIONS) for part in lower_parts):
        return False
    if len(parts) == 1:
        return name in root_files or name in _ROOT_EXTRA

    family = parts[0]
    suffix = PurePosixPath(name).suffix.lower()
    if family in _WORKER_SUFFIXES and len(parts) == 2:
        return suffix in _WORKER_SUFFIXES[family] or name == "udb/build_info.json"
    if family == "THIRD_PARTY_NOTICES" and len(parts) >= 3:
        leaf = lower_parts[-1]
        return (lower_parts[1].endswith(".dist-info")
                and any(word in leaf for word in ("license", "copying", "notice"))
                and suffix in ("", ".txt", ".md", ".rst", ".html"))
    if family != "vendor":
        return False

    if len(parts) == 2:
        return parts[1] in {"tunnel-client.exe", "tunnel-LICENSES.txt", "DEPENDENCIES.json"}
    if parts[1] != "python-runtime":
        return False
    if len(parts) == 3:
        return parts[2] in _RUNTIME_FILES
    if len(parts) == 4 and parts[2] == "DLLs":
        return suffix in (".dll", ".pyd")
    if len(parts) >= 4 and parts[2] == "Lib":
        return suffix == ".py"
    return False


def assert_safe_payload(content: bytes) -> None:
    """Reject nested ZIP payloads and high-confidence credential signatures.

    Raise without returning or logging any portion of the secret.
    """
    if zipfile.is_zipfile(io.BytesIO(content)):
        raise ValueError("Nested ZIP payload is forbidden")
    if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
        raise ValueError("Possible credential in release payload")
