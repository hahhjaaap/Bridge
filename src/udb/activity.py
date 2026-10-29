"""Bounded, metadata-only tool activity and a local console viewer."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

from .core import PolicyError, reject_links
from .outcomes import native_status, report_outcome

KEEP_ROWS = 2000
MAX_DETAIL = 2048
SAFE_FIELDS = frozenset(
    {
        "action",
        "mode",
        "project",
        "path",
        "source",
        "destination",
        "cwd",
        "program",
        "language",
        "encoding",
        "offset",
        "limit",
        "range",
        "job_id",
        "session_id",
        "window_id",
        "pid",
        "status",
        "state",
        "exit_code",
        "bytes",
        "size_bytes",
        "next_offset",
        "has_more",
        "timeout_seconds",
        "wait_ms",
        "operation",
        "version",
    }
)
HIDDEN_FIELDS = frozenset(
    {"text", "content", "code", "script", "value", "data", "stdin", "sheets"}
)
DETAIL_FIELDS = frozenset(
    {"arguments_count", "content_blocks", "error_type", "details_truncated"}
) | {key + "_hidden" for key in HIDDEN_FIELDS}


def clean(value, limit=240):
    text = str(value)
    text = re.sub(r"(?i)\b(?:sk|sess)-[a-z0-9_-]{8,}", "[redacted]", text)
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
    )
    # Strip terminal controls, line injection and bidi formatting from metadata.
    text = "".join(
        c
        for c in text
        if c.isprintable()
        and c not in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
    )
    return text[:limit] + ("..." if len(text) > limit else "")


def argument_details(arguments):
    if not isinstance(arguments, dict):
        return {}
    result = {}
    for key, value in arguments.items():
        if key in SAFE_FIELDS and isinstance(
            value, (str, int, float, bool, type(None))
        ):
            result[key] = clean(value) if isinstance(value, str) else value
        elif key in HIDDEN_FIELDS:
            result[key + "_hidden"] = True
        elif key == "arguments" and isinstance(value, list):
            result["arguments_count"] = len(value)
    return result


def completion(result):
    if not isinstance(result, dict):
        return "ERROR", {}
    if "_upstream_rpc_error" in result:
        return "ERROR", {"error_type": "UpstreamRPCError"}
    report = result.get("structuredContent")
    blocks = result.get("content", [])
    blocks = blocks if isinstance(blocks, list) else []
    first = next(
        (
            b.get("text", "")
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ),
        "",
    )
    if not isinstance(report, dict):
        report = {}
        for block in blocks:
            text = block.get("text", "") if isinstance(block, dict) else ""
            if isinstance(text, str) and len(text) <= 65536:
                try:
                    candidate = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if isinstance(candidate, dict):
                    report = candidate
                    break
    state = report_outcome(report).status
    native = native_status(first) if isinstance(first, str) else None
    if result.get("isError") is True or native == "ERROR":
        state = "ERROR"
    elif state != "ERROR" and (native == "RUNNING" or state == "RUNNING"):
        state = "RUNNING"
    details = argument_details(report)
    details["content_blocks"] = len(blocks)
    return state, details


def activity_path(home):
    path = Path(home).absolute() / "state" / "activity.sqlite3"
    for name in (
        str(path),
        str(path) + "-journal",
        str(path) + "-wal",
        str(path) + "-shm",
    ):
        reject_links(Path(name))
    if path.exists() and path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Activity file exceeds its size limit")
    return path


class Activity:
    _lock = threading.RLock()

    def __init__(self, home):
        self.home = Path(home)

    def emit(self, kind, tool, call_id="", status="", elapsed_ms=None, details=None):
        """Never fail a tool call or echo exceptions if telemetry is unavailable."""
        try:
            with self._lock:
                path = activity_path(self.home)
                path.parent.mkdir(parents=True, exist_ok=True)
                reject_links(path.parent)
                safe = {}
                for key, value in (details or {}).items():
                    if key in SAFE_FIELDS | DETAIL_FIELDS and isinstance(
                        value, (str, int, float, bool, type(None))
                    ):
                        safe[key] = clean(value) if isinstance(value, str) else value
                detail = json.dumps(safe, ensure_ascii=True, allow_nan=False)
                if len(detail) > MAX_DETAIL:
                    detail = json.dumps({"details_truncated": True})
                with closing(sqlite3.connect(path, timeout=0.1)) as db, db:
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS activity (seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT, tool TEXT, call_id TEXT, status TEXT, elapsed_ms INTEGER, details TEXT)"
                    )
                    db.execute(
                        "INSERT INTO activity(at,kind,tool,call_id,status,elapsed_ms,details) VALUES (?,?,?,?,?,?,?)",
                        (
                            time.time(),
                            clean(kind, 20),
                            clean(tool, 80),
                            clean(call_id, 20),
                            clean(status, 20),
                            elapsed_ms,
                            detail,
                        ),
                    )
                    db.execute(
                        "DELETE FROM activity WHERE seq <= (SELECT MAX(seq) FROM activity) - ?",
                        (KEEP_ROWS,),
                    )
                if os.name != "nt":
                    path.chmod(0o600)
        except (OSError, ValueError, TypeError, PolicyError, sqlite3.Error):
            pass


class EventReader:
    """Skip unchanged idle polls without holding a database handle or read lock.

    Database/WAL metadata detects ordinary writes and rotation. A periodic
    query bounds delays on filesystems whose metadata updates are deferred.
    Paths are validated even when a poll can use the cached high-water mark.
    """

    RESCAN_SECONDS = 2.0

    def __init__(self, home):
        self.home = home
        self.close()

    def close(self):
        self.signature = None
        self.caught_up_after = None
        self.next_scan = 0.0

    @staticmethod
    def _signature(path):
        result = []
        for file in (path, path.with_name(path.name + "-wal")):
            try:
                info = file.stat()
            except FileNotFoundError:
                result.append(None)
            else:
                result.append(
                    (
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )
                )
        return tuple(result)

    def read(self, *, after=None, limit=30):
        if not 0 <= limit <= 2000:
            raise ValueError("Log tail must be between 0 and 2000")
        path = activity_path(self.home)
        signature = self._signature(path)
        if signature[0] is None:
            self.close()
            return []
        if limit == 0:
            return []
        now = time.monotonic()
        if (
            after is not None
            and after == self.caught_up_after
            and signature == self.signature
            and now < self.next_scan
        ):
            return []
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        ) as db:
            db.row_factory = sqlite3.Row
            try:
                if after is None:
                    rows = db.execute(
                        "SELECT * FROM activity ORDER BY seq DESC LIMIT ?", (limit,)
                    ).fetchall()
                    rows.reverse()
                else:
                    rows = db.execute(
                        "SELECT * FROM activity WHERE seq > ? ORDER BY seq LIMIT ?",
                        (after, limit),
                    ).fetchall()
                    if not rows:
                        newest = db.execute("SELECT MAX(seq) FROM activity").fetchone()[
                            0
                        ]
                        if newest is not None and newest < after:
                            rows = db.execute(
                                "SELECT * FROM activity ORDER BY seq LIMIT ?", (limit,)
                            ).fetchall()
            except sqlite3.OperationalError as exc:
                if "no such table" in str(exc):
                    return []
                raise
        # Retain the pre-query signature: a concurrent write must invalidate
        # the next poll, even when it happened just after this SELECT finished.
        self.signature = signature
        self.next_scan = now + self.RESCAN_SECONDS
        self.caught_up_after = (
            (rows[-1]["seq"] if rows else after or 0)
            if after is None or len(rows) < limit
            else None
        )
        result = []
        for row in rows:
            value = dict(row)
            value["details"] = json.loads(value["details"])
            result.append(value)
        return result


def read_events(home, *, after=None, limit=30):
    """One-shot query; live viewers should retain an EventReader instead."""
    with closing(EventReader(home)) as reader:
        return reader.read(after=after, limit=limit)


def console_color(output):
    if not output.isatty() or "NO_COLOR" in os.environ:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        handle = kernel.GetStdHandle(-11 & 0xFFFFFFFF)
        mode = wintypes.DWORD()
        return bool(
            kernel.GetConsoleMode(handle, ctypes.byref(mode))
            and kernel.SetConsoleMode(handle, mode.value | 4)
        )
    return True


def render_event(row, *, details=False, color=False, emoji=True):
    stamp = time.strftime("%H:%M:%S", time.localtime(row["at"]))
    state = "CALL" if row["kind"] == "call" else row["status"] or "INFO"
    suffix = ""
    if row["kind"] == "result":
        suffix = {"RUNNING": " returned; task still running", "ERROR": " failed"}.get(
            state, " completed"
        )
        suffix += f" ({row['elapsed_ms']} ms)"
    symbol = (
        {"CALL": "📞", "PASS": "✅", "ERROR": "❌", "RUNNING": "⏳"}.get(state, "ℹ️")
        if emoji
        else ""
    )
    label = f"{symbol} {state:7}" if symbol else f"{state:7}"
    if color:
        code = {"CALL": 36, "PASS": 32, "ERROR": 31, "RUNNING": 33}.get(state, 37)
        label = f"\x1b[{code}m{label}\x1b[0m"
    value = f"[{stamp}] {label} {clean(row['tool'], 80)}{suffix} #{clean(row['call_id'], 20)}".rstrip(
        " #"
    )
    if details and row.get("details"):
        value += "\n           " + json.dumps(row["details"], ensure_ascii=False)
    return value


def watch(home, *, tail=30, once=False, details=False, as_json=False, output=None):
    output = output or sys.stdout
    color = console_color(output) if not as_json else False
    try:
        "🚀📞✅❌⏳".encode(getattr(output, "encoding", None) or "utf-8")
        emoji = True
    except (LookupError, UnicodeError):
        emoji = False
    if not as_json:
        print(
            ("🚀 " if emoji else "")
            + "UDB live tool activity | 10 parallel calls / 10 background jobs | Ctrl+C closes this viewer",
            file=output,
            flush=True,
        )
        print(
            "Payloads/command arguments are hidden; --details shows safe metadata.",
            file=output,
            flush=True,
        )
    last = 0
    try:
        with closing(EventReader(home)) as reader:
            rows = reader.read(limit=tail)
            # --tail 0 follows only newly arriving rows, not retained history.
            if tail == 0:
                newest = reader.read(limit=1)
                last = newest[0]["seq"] if newest else 0
            while True:
                for row in rows:
                    print(
                        json.dumps(row, ensure_ascii=True)
                        if as_json
                        else render_event(
                            row, details=details, color=color, emoji=emoji
                        ),
                        file=output,
                        flush=True,
                    )
                    last = row["seq"]
                if once:
                    return 0
                time.sleep(0.3)
                rows = reader.read(after=last, limit=200)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, PolicyError, sqlite3.Error):
        print(
            "UDB: Cannot read activity log; check its local path and integrity.",
            file=sys.stderr,
        )
        return 2
