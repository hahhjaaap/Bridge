"""Single companion tool router.

The runtime owns sessions and lifecycle; features own long-running jobs;
extensions own optional file/terminal helpers. This module is the only
dispatcher for the core file and terminal tools. Schemas remain in schemas.py.
"""

from __future__ import annotations

from typing import Any

from .constants import MAX_OUTPUT_CHARS
from .extensions import EXTENSION_HANDLERS, call_extension
from .features import NAMES
from .responses import respond_text
from .schemas import TOOLS


def _terminal_start(runtime, args):
    r = runtime.start(
        args["program"],
        args.get("arguments", []),
        args.get("cwd", "."),
        args.get("wait_ms", 150),
        args.get("timeout_seconds", 3600),
        args.get("memory_mib", 1024),
        args.get("mode", "pipe"),
        args.get("columns", 100),
        args.get("rows", 30),
    )
    warning = (
        "\nWarning: transcript persistence failed after process startup; use this session ID to inspect/stop it; do not replay the command."
        if not r["persistence_available"]
        else ""
    )
    response = respond_text(
        r["state"],
        "Lệnh đã bắt đầu" if r["state"] == "RUNNING" else "Lệnh đã hoàn tất",
        f"Phiên: {r['session_id']} | Exit: {r['exit_code']} | Next offset: {r['next_offset']}\n{r['output'][:MAX_OUTPUT_CHARS]}"
        + warning,
    )
    response["structuredContent"] = r
    return response


def _terminal_session(runtime, name, args):
    ident = args["session_id"]
    if ident not in runtime.sessions:
        raise ValueError("Unknown session; sessions are lost when server restarts")
    session = runtime.sessions[ident]
    if name == "terminal_send":
        r = session.send(args["text"])
        return respond_text(
            "PASS", "Đã gửi dữ liệu vào phiên terminal", f"Phiên: {r['session_id']}"
        )
    if name == "terminal_stop":
        r = session.stop()
        return respond_text(
            "PASS",
            "Đã dừng phiên terminal",
            f"Phiên: {r['session_id']} | Exit: {r['exit_code']}",
        )
    r = session.read(args.get("offset"), args.get("wait_ms", 100))
    headline = (
        "Tiến trình còn chạy"
        if r["state"] == "RUNNING"
        else "Tiến trình hoàn tất"
        if r["state"] == "PASS"
        else "Tiến trình lỗi"
    )
    note = "[Earlier output expired]\n" if r["output_truncated"] else ""
    return respond_text(
        r["state"],
        headline,
        f"Phiên: {r['session_id']} | Exit: {r['exit_code']} | Next offset: {r['next_offset']} | More: {r['has_more']}\n"
        + note
        + r["output"][:MAX_OUTPUT_CHARS],
    )


def _file_list(runtime, args):
    r = runtime.files.list(args.get("path", "."), args.get("limit", 100))
    lines = [f"{entry['kind']:4} {entry['name']}" for entry in r["items"]]
    return respond_text(
        "PASS", f"Đã liệt kê {len(lines)} mục trong {r['path']}", "\n".join(lines)
    )


def _file_read(runtime, args):
    r = runtime.files.read(args["path"], args.get("offset", 0), args.get("limit", 8192))
    response = respond_text("PASS", f"Đã đọc {r['path']} ({r['size']} bytes)")
    response["content"][0]["text"] += (
        f"\nSHA256: {r['sha256']} | Next offset: {r['next_offset']} | Remaining: {r['remaining']}\n{r['content']}"
    )
    response["structuredContent"] = r
    return response


def _file_write(runtime, args):
    r = runtime.files.write(
        args["path"], args["content"], args.get("expected_sha256", "NEW")
    )
    return respond_text(
        "PASS",
        "Đã tạo file" if r["created"] else "Đã cập nhật file",
        f"{r['path']} | Bytes: {r['bytes']} | SHA256: {r['sha256']}",
    )


def _file_replace(runtime, args):
    r = runtime.files.replace(
        args["path"],
        args["old"],
        args["new"],
        args["expected_sha256"],
        args.get("occurrences", 1),
    )
    return respond_text(
        "PASS",
        f"Đã sửa file ({r['replacements']} vị trí)",
        f"{r['path']} | SHA256: {r['sha256']}",
    )


def _file_search(runtime, args):
    r = runtime.files.search(
        args.get("path", "."),
        args.get("pattern", "*"),
        args.get("text", ""),
        args.get("limit", 30),
    )
    return respond_text(
        "PASS",
        f"Tìm thấy {len(r['matches'])} file",
        "\n".join(r["matches"])
        + f"\nScanned: {r['scanned']} | Truncated: {r['truncated']}",
    )


# All native companion routes are declared once. Extensions and background
# features keep their own dispatchers because their lifecycle differs.
NATIVE_HANDLERS = {
    "terminal_start": _terminal_start,
    "terminal_read": _terminal_session,
    "terminal_send": _terminal_session,
    "terminal_stop": _terminal_session,
    "file_list": _file_list,
    "file_read": _file_read,
    "file_write": _file_write,
    "file_replace": _file_replace,
    "file_search": _file_search,
}


def validate_catalog() -> None:
    """Fail when a tool is advertised without a handler (or vice versa)."""
    advertised = [item["name"] for item in TOOLS]
    registered = set(NATIVE_HANDLERS) | set(EXTENSION_HANDLERS) | set(NAMES)
    if len(advertised) != len(set(advertised)):
        raise RuntimeError("Duplicate companion tool schemas")
    if set(advertised) != registered:
        missing = sorted(set(advertised) - registered)
        extra = sorted(registered - set(advertised))
        raise RuntimeError(
            f"Companion schema/handler mismatch: missing={missing}, extra={extra}"
        )


def dispatch_tool(runtime, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Route a single tool call; never retry or widen its permissions."""
    if name in EXTENSION_HANDLERS:
        return call_extension(runtime, name, args)
    if name in NAMES:
        return runtime.features.tool(name, args)
    handler = NATIVE_HANDLERS.get(name)
    if handler is None:
        raise ValueError("Unknown tool")
    if name in {"terminal_read", "terminal_send", "terminal_stop"}:
        return handler(runtime, name, args)
    return handler(runtime, args)
