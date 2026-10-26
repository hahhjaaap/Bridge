"""UDB Companion V2 incremental file and terminal tools.

The module deliberately does not change the installed UDB executable,
permissions, or MCP routing. File operations remain inside ScopedFiles.root.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .responses import respond_text

MAX_HASH = 256 * 1024 * 1024
MAX_LINE_FILE = 8 * 1024 * 1024
MAX_CONTEXT_SCAN = 20000
MAX_CONTEXT_RESULTS = 100
MAX_TEXT_FILE = 1024 * 1024


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_destination(files, value: str) -> Path:
    result = files.path(value, allow_missing=True)
    if result.exists() or result.is_symlink():
        raise FileExistsError("Destination already exists; overwrite is disabled")
    parent = files.path(str(result.parent), allow_missing=True)
    if not parent.is_dir():
        raise FileNotFoundError("Create parent directory first with file_mkdir")
    return result


def _source_file(files, value: str, expected_sha256: str = "") -> Path:
    p = files.path(value)
    if not p.is_file():
        raise ValueError("Source must be a regular file")
    if p.stat().st_size > MAX_HASH:
        raise ValueError("File exceeds 256 MiB verification limit")
    if expected_sha256:
        if len(expected_sha256) != 64 or any(
            ch not in "0123456789abcdefABCDEF" for ch in expected_sha256
        ):
            raise ValueError("Expected SHA256 must be 64 hex characters")
        if _sha256(p).lower() != expected_sha256.lower():
            raise ValueError("Source changed; operation cancelled")
    return p


def _file_stat(rt, a):
    p = rt.files.path(a["path"])
    stat = p.stat()
    relative = str(p.relative_to(rt.files.root))
    if p.is_file():
        if stat.st_size > MAX_HASH:
            return (
                f"File: {relative}\nBytes: {stat.st_size}\nSHA256: skipped (>256 MiB)"
            )
        return f"File: {relative}\nBytes: {stat.st_size}\nSHA256: {_sha256(p)}"
    if p.is_dir():
        return f"Directory: {relative}"
    return f"Other: {relative}"


def _file_mkdir(rt, a):
    p = rt.files.path(a["path"], allow_missing=True)
    if p == rt.files.root:
        raise FileExistsError("Root already exists")
    if p.exists() or p.is_symlink():
        raise FileExistsError("Directory already exists")
    parent = rt.files.path(str(p.parent), allow_missing=True)
    if not parent.is_dir():
        raise FileNotFoundError("Parent directory not found")
    p.mkdir(exist_ok=False)
    return "Created directory: " + str(p.relative_to(rt.files.root))


def _file_copy(rt, a):
    source = _source_file(rt.files, a["source"], a.get("expected_sha256", ""))
    destination = _safe_destination(rt.files, a["destination"])
    expected = _sha256(source)
    created = False
    identity = None
    try:
        with source.open("rb") as src, destination.open("xb") as dst:
            created = True
            stat = os.fstat(dst.fileno())
            identity = (stat.st_dev, stat.st_ino)
            shutil.copyfileobj(src, dst, 1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if _sha256(destination) != expected:
            raise IOError("Copied file checksum mismatch")
    except Exception:
        # Do not delete a concurrently created file owned by another process.
        if created and identity is not None:
            try:
                stat = destination.stat()
                if (stat.st_dev, stat.st_ino) == identity:
                    destination.unlink()
            except FileNotFoundError:
                pass
        raise
    return f"Copied {source.relative_to(rt.files.root)} to {destination.relative_to(rt.files.root)}\nSHA256: {expected}"


def _file_move(rt, a):
    source = _source_file(rt.files, a["source"], a.get("expected_sha256", ""))
    destination = _safe_destination(rt.files, a["destination"])
    expected = _sha256(source)
    # UDB currently runs only on Windows, where os.rename fails if dst exists.
    # Do not silently substitute os.replace, which would overwrite.
    if os.name != "nt":
        raise RuntimeError("Atomic no-replace move is supported on Windows only")
    source.rename(destination)
    if _sha256(destination) != expected:
        raise IOError(
            "Moved file checksum mismatch: inspect destination before retrying"
        )
    return f"Moved {source.relative_to(rt.files.root)} to {destination.relative_to(rt.files.root)}\nSHA256: {expected}"


def _file_read_lines(rt, a):
    p = _source_file(rt.files, a["path"])
    if p.stat().st_size > MAX_LINE_FILE:
        raise ValueError("Read-lines limit: 8 MiB; use file_read byte paging")
    start = max(0, int(a.get("offset", 0)))
    length = max(1, min(250, int(a.get("length", 50))))
    lines = p.read_text(encoding="utf-8").splitlines()
    selected = lines[start : start + length]
    numbered = [f"{start + n + 1}: {line[:1000]}" for n, line in enumerate(selected)]
    data = "\n".join(numbered)
    cropped = len(data) > 11000
    if cropped:
        data = data[:11000]
    return (
        f"Lines: {start + 1}-{start + len(selected)} of {len(lines)} | "
        f"Next offset: {start + len(selected)} | Output clipped: {cropped}\n" + data
    )


def _file_search_context(rt, a):
    root = rt.files.path(a.get("path", "."))
    if not root.is_dir():
        raise NotADirectoryError("Search root must be a folder")
    text = a["text"]
    if not isinstance(text, str) or not text or len(text) > 200:
        raise ValueError("Provide a literal text query of 1-200 characters")
    pattern = str(a.get("pattern", "*"))
    context = max(0, min(3, int(a.get("context", 1))))
    limit = max(1, min(MAX_CONTEXT_RESULTS, int(a.get("limit", 20))))
    matches, scanned, truncated = [], 0, False
    hidden = {
        x.casefold()
        for x in (
            "node_modules",
            ".git",
            ".venv",
            "venv",
            "__pycache__",
            ".udb-recovery",
        )
    }
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            x
            for x in dirs
            if x.casefold() not in hidden and _allowed(rt.files, Path(folder) / x)
        ]
        for filename in names:
            scanned += 1
            if scanned > MAX_CONTEXT_SCAN:
                truncated = True
                break
            if not fnmatch.fnmatch(filename, pattern):
                continue
            candidate = Path(folder) / filename
            if not _allowed(rt.files, candidate):
                continue
            path = rt.files.path(str(candidate))
            if not path.is_file() or path.stat().st_size > MAX_TEXT_FILE:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (UnicodeError, OSError):
                continue
            for idx, row in enumerate(lines):
                if text.casefold() not in row.casefold():
                    continue
                snippet = "\n".join(
                    f"{j + 1}: {lines[j][:350]}"
                    for j in range(
                        max(0, idx - context), min(len(lines), idx + context + 1)
                    )
                )
                matches.append(
                    f"{path.relative_to(rt.files.root)}:{idx + 1}\n{snippet}"
                )
                if len(matches) >= limit:
                    truncated = True
                    break
            if truncated:
                break
        if truncated:
            break
    result = "\n---\n".join(matches)
    cropped = len(result) > 10500
    if cropped:
        result = result[:10500]
    return (
        f"Matches: {len(matches)} | Files scanned: {scanned} | "
        f"Truncated: {truncated or cropped}\n" + result
    )


def _allowed(files, path):
    try:
        files.path(str(path))
        return True
    except (ValueError, PermissionError, FileNotFoundError, OSError):
        return False


def _terminal_list(rt, a):
    lines = []
    for sid, session in rt.sessions.items():
        code = session.process.poll()
        state = "RUNNING" if code is None else ("PASS" if code == 0 else "ERROR")
        lines.append(f"{sid} | {state} | exit={code}")
    return "No companion-owned terminal sessions." if not lines else "\n".join(lines)


def _terminal_wait(rt, a):
    sid = a["session_id"]
    session = rt.sessions.get(sid)
    if session is None:
        raise ValueError("Unknown terminal session")
    timeout = max(0, min(1500, int(a.get("timeout_ms", 500))))
    end = time.monotonic() + timeout / 1000
    while session.process.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    row = session.read(a.get("offset"), 0)
    if row["state"] == "RUNNING":
        status = "RUNNING"
    else:
        status = row["state"]
    return status, (
        f"Session: {sid} | Exit: {row['exit_code']} | Next offset: {row['next_offset']} | "
        f"More: {row['has_more']}\n{row['output'][:11000]}"
    )


EXTENSION_TOOLS = [
    {
        "name": "file_stat",
        "description": "Scoped file/directory metadata with SHA256 for files up to 256 MiB.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "file_mkdir",
        "description": "Create one directory inside the configured project. No overwrite.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "file_copy",
        "description": "Copy one project file to a new path, SHA256 verified; never overwrite.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["source", "destination"],
        },
    },
    {
        "name": "file_move",
        "description": "Move one project file to a new path with guarded no-overwrite semantics on Windows.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {"type": "string"},
                "destination": {"type": "string"},
                "expected_sha256": {"type": "string"},
            },
            "required": ["source", "destination"],
        },
    },
    {
        "name": "file_read_lines",
        "description": "Read project text file by 0-based line offset and max 250 lines.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "offset": {"type": "integer"},
                "length": {"type": "integer"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "file_search_context",
        "description": "Search literal text inside scoped project files, returning contextual line numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "pattern": {"type": "string"},
                "text": {"type": "string"},
                "context": {"type": "integer"},
                "limit": {"type": "integer"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "terminal_list",
        "description": "List only terminal sessions created by this companion, with completion status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "terminal_wait",
        "description": "Wait briefly for a companion terminal job to complete; return PASS/RUNNING/ERROR.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "timeout_ms": {"type": "integer"},
                "offset": {"type": "integer"},
            },
            "required": ["session_id"],
        },
    },
]

EXTENSION_HANDLERS = {
    "file_stat": _file_stat,
    "file_mkdir": _file_mkdir,
    "file_copy": _file_copy,
    "file_move": _file_move,
    "file_read_lines": _file_read_lines,
    "file_search_context": _file_search_context,
    "terminal_list": _terminal_list,
    "terminal_wait": _terminal_wait,
}


def call_extension(runtime, name: str, args: dict[str, Any], respond=respond_text):
    """Dispatch a registered extension without modifying the Runtime class."""
    handler = EXTENSION_HANDLERS[name]
    try:
        result = handler(runtime, args)
        if isinstance(result, tuple):
            state, description = result
        else:
            state, description = "PASS", result
        headline = {"PASS": "Hoàn thành", "RUNNING": "Đang chạy", "ERROR": "Lỗi"}[state]
        return respond(state, headline, description)
    except (OSError, KeyError, ValueError, RuntimeError, UnicodeError) as exc:
        return respond("ERROR", f"{type(exc).__name__}: {exc}")


def install(Runtime, ScopedFiles, respond_text):
    """Compatibility adapter for old callers; canonical Runtime dispatches directly."""
    old = Runtime.tool
    if getattr(old, "_udb_extension_dispatch", False):
        return

    def tool_with_extensions(self, name: str, args: dict[str, Any]):
        if name in EXTENSION_HANDLERS:
            return call_extension(self, name, args, respond_text)
        return old(self, name, args)

    tool_with_extensions._udb_extension_dispatch = True
    Runtime.tool = tool_with_extensions
