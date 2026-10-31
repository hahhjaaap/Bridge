"""UDB Unified MCP proxy: existing UDB tool set + local UDB Companion.

No GUI or privilege changes. One stdio MCP endpoint. Python stdlib only.
Existing owner grants/state stay untouched; core and Companion share this package.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
import uuid
from typing import Any

from udb._version import VERSION
from udb.catalog import public_catalog, resolve_call
from udb.concurrency import MAX_CONCURRENT
from udb.outcomes import ERROR_PREFIXES, native_status, report_from_text, report_outcome
from udb.recovery import RecoveringChild
from udb.transport import (
    MAX_LINE,
    StdioChild,
    compact_json,
    rpc_error,
)


def normalized_output(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """Classify tool completion conservatively; preserve the original backend result.

    Native tool errors must never be presented as PASS. A successful *tool call*
    does not imply an asynchronous job has finished.
    """
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    value = copy.deepcopy(result)
    content = value["content"]
    text_blocks = [
        i.get("text", "")
        for i in content
        if isinstance(i, dict)
        and i.get("type") == "text"
        and isinstance(i.get("text"), str)
    ]
    first = text_blocks[0].strip() if text_blocks else ""
    first_line = first.splitlines()[0] if first else ""
    native = native_status(first_line)
    if native == "ERROR":
        value["isError"] = True
        return value
    report = value.get("structuredContent")
    if not isinstance(report, dict):
        report = None
        for text in text_blocks:
            candidate = report_from_text(text)
            if isinstance(candidate, dict):
                report = candidate
                break
    if native:
        if (
            value.get("isError") is True
            or report_outcome(report or {}).status == "ERROR"
        ):
            value["isError"] = True
            value["content"] = [
                {
                    "type": "text",
                    "text": f"ERROR | Backend reported a tool error ({tool})",
                }
            ] + content
        return value

    state, label = report_outcome(report or {})
    if value.get("isError") is True:
        state, label = "ERROR", "Backend reported a tool error"
    elif first_line.startswith(ERROR_PREFIXES):
        state, label = "ERROR", "Backend returned an error message"
    value["content"] = [
        {"type": "text", "text": f"{state} | {label} ({tool})"}
    ] + content
    if state == "ERROR":
        value["isError"] = True
    return value


class UnifiedProxy:
    def __init__(self, primary: StdioChild, companion: StdioChild, activity=None):
        self.primary = primary
        self.companion = companion
        self.send_lock = threading.Lock()
        self.tool_routes: dict[str, StdioChild] = {}
        self.tool_cache: list[dict[str, Any]] | None = None
        self.route_lock = threading.Lock()
        self.startup_ok = False
        self.closed = threading.Event()
        self.active_limit = threading.BoundedSemaphore(MAX_CONCURRENT)
        self.initialization_lock = threading.Lock()
        self.activity = activity
        self._pointer_presence_started = False
        self._pointer_retry_after = 0.0

    def activity_event(
        self,
        kind,
        tool,
        call_id,
        *,
        result=None,
        error=None,
        elapsed_ms=None,
        arguments=None,
    ):
        observer = getattr(self, "activity", None)
        if observer is None:
            return
        try:
            from .activity import argument_details, completion

            if kind == "call":
                state, details = "CALL", argument_details(arguments)
            elif error is not None:
                state, details = "ERROR", {"error_type": type(error).__name__}
            else:
                state, details = completion(result)
            observer.emit(kind, tool, call_id, state, elapsed_ms, details)
        except Exception:
            # Observation must never change dispatch, responses or permissions.
            pass

    def send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            data = compact_json(message)
            if len(data) > MAX_LINE:
                # Never silently discard a response: the caller would hang and
                # might retry an already-executed, side-effecting operation.
                if "id" not in message:
                    return  # oversized notification has no response contract
                data = compact_json(
                    rpc_error(
                        message["id"],
                        "MCP response exceeds size limit; do not replay side effects",
                        -32005,
                    )
                )
                if len(data) > MAX_LINE:
                    raise ValueError("MCP response ID is too large")
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    def initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        with self.initialization_lock:
            p = self.primary.request("initialize", params)
            c = self.companion.request("initialize", params)
            if p.get("error") or c.get("error"):
                raise RuntimeError("Backend MCP initialization rejected")
            base = copy.deepcopy(p.get("result", {}))
            if not isinstance(base, dict):
                raise RuntimeError("Invalid MCP initialization result")
            caps = base.setdefault("capabilities", {})
            caps.setdefault("tools", {})["listChanged"] = False
            base["serverInfo"] = {"name": "UDB Unified", "version": VERSION}
            if not self.startup_ok:
                threading.Thread(
                    target=self._watch_access, daemon=True, name="UDB grant watcher"
                ).start()
            self.startup_ok = True
            return base

    def _watch_access(self):
        while not self.closed.wait(1):
            try:
                reply = self.primary.request(
                    "tools/call",
                    {"name": "computer_status", "arguments": {}},
                    timeout=5,
                )
                result = reply.get("result", {})
                grant = result.get("structuredContent")
                if grant is None:
                    grant = json.loads(
                        next(
                            b["text"]
                            for b in result.get("content", [])
                            if b.get("type") == "text"
                        )
                    )
                if (
                    result.get("isError")
                    or not isinstance(grant, dict)
                    or grant.get("enabled") is not True
                ):
                    self.companion.request("udb/close-sessions", timeout=10)
                    self._pointer_presence_started = False
                elif "desktop" not in grant.get("capabilities", []):
                    self.companion.request("udb/close-desktop", timeout=10)
                    self._pointer_presence_started = False
                elif grant.get("desktop_paused"):
                    if self._pointer_presence_started:
                        self.companion.request("udb/close-desktop", timeout=10)
                    self._pointer_presence_started = False
                    self._pointer_retry_after = 0.0
                elif (
                    not self._pointer_presence_started
                    and time.monotonic() >= self._pointer_retry_after
                ):
                    # Visual presence is automatic only after an existing
                    # owner desktop grant is verified. No input injection.
                    self._pointer_retry_after = time.monotonic() + 10
                    try:
                        reply = self.companion.request("udb/show-pointer", timeout=15)
                        self._pointer_presence_started = (
                            not reply.get("error")
                            and reply.get("result", {}).get("visible") is True
                        )
                    except (RuntimeError, TimeoutError, OSError):
                        # Visual decoration is optional. Never close work
                        # sessions because a pointer overlay failed to start.
                        pass
            except (
                ValueError,
                KeyError,
                StopIteration,
                TypeError,
                RuntimeError,
                TimeoutError,
                OSError,
            ):
                try:
                    self.companion.request("udb/close-sessions", timeout=10)
                    self._pointer_presence_started = False
                except (RuntimeError, TimeoutError, OSError):
                    # The cleanup backend can be temporarily unavailable.
                    # Keep watching so the next cycle can recheck the owner
                    # grant and retry closing sessions. Exiting here would
                    # permanently disable revocation monitoring for this proxy.
                    self._pointer_presence_started = False
                    continue

    def tools_list(self) -> dict[str, Any]:
        # Backends advertise tools.listChanged=False. Discovery is immutable
        # during this proxy session; cache schemas rather than repeat 2 RPCs
        # per tools/list request. Serialize concurrent first discoveries.
        with self.route_lock:
            if self.tool_cache is not None:
                return {"tools": copy.deepcopy(self.tool_cache)}
            result: list[dict[str, Any]] = []
            routes: dict[str, StdioChild] = {}
            for backend in (self.primary, self.companion):
                response = backend.request("tools/list")
                if response.get("error"):
                    raise RuntimeError(f"Cannot discover {backend.name} MCP tools")
                tools = response.get("result", {}).get("tools")
                if not isinstance(tools, list):
                    raise RuntimeError("MCP backend returned invalid tool list")
                for tool in tools:
                    name = tool.get("name") if isinstance(tool, dict) else None
                    if not isinstance(name, str) or not name or name in routes:
                        raise RuntimeError("Duplicate or malformed MCP tool name")
                    routes[name] = backend
                    result.append(tool)
            catalog = public_catalog(result, routes)
            self.tool_routes = routes
            self.tool_cache = catalog
            return {"tools": copy.deepcopy(self.tool_cache)}

    def call_tool(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise ValueError("Tool name must be a string")
        public_name = name
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object")
        # Refresh tool routing on first invocation. Do not retry side effects.
        if not self.tool_routes:
            self.tools_list()
        if name not in self.tool_routes and name not in {
            t["name"] for t in self.tool_cache or []
        }:
            raise ValueError("Unknown MCP tool")
        name, arguments = resolve_call(name, arguments)
        params = {**params, "name": name, "arguments": arguments}
        backend = self.tool_routes.get(name)
        if backend is None:
            raise ValueError("Unknown MCP tool")
        if backend is self.companion:
            access = self.primary.request(
                "tools/call", {"name": "computer_status", "arguments": {}}
            )
            result = access.get("result", {})
            grant = result.get("structuredContent")
            if grant is None:
                try:
                    grant = json.loads(
                        next(
                            b["text"]
                            for b in result.get("content", [])
                            if b.get("type") == "text"
                        )
                    )
                except (ValueError, KeyError, StopIteration, TypeError):
                    grant = {}
            if (
                result.get("isError")
                or not isinstance(grant, dict)
                or grant.get("enabled") is not True
            ):
                self.companion.request("udb/close-sessions", timeout=10)
                raise RuntimeError(
                    "Existing UDB owner computer access is required; AI cannot grant it"
                )
            arguments = params.get("arguments", {})
            if name == "virtual_pointer" and "desktop" not in grant.get(
                "capabilities", []
            ):
                raise RuntimeError("Existing desktop capability is required")
            if (
                name == "virtual_pointer"
                and grant.get("desktop_paused")
                and arguments.get("action") not in {"status", "hide", "stop"}
            ):
                raise RuntimeError(
                    "Desktop control is paused locally; only the owner can resume in UDB"
                )
            if (
                name == "terminal_start"
                and arguments.get("mode") == "conpty"
                and "execute" not in grant.get("capabilities", [])
            ):
                raise RuntimeError("Existing execute capability is required")
            job_control = (
                (
                    name == "file_operation"
                    and arguments.get("action") in ("read", "list", "cancel")
                )
                or (name == "terminal_interact" and not arguments.get("text"))
                or (
                    name == "file_upload"
                    and arguments.get("action") in ("status", "abort")
                )
                or (
                    name == "virtual_pointer"
                    and arguments.get("action") in ("hide", "stop", "status")
                )
                or (
                    name == "work_recovery"
                    and arguments.get("action", "list") != "resume"
                )
            )
            if not job_control and name not in {
                "terminal_stop",
                "terminal_read",
                "terminal_list",
                "terminal_wait",
                "search_read",
                "search_stop",
                "system_process_terminate",
            }:
                host = self.primary.request(
                    "tools/call", {"name": "host_status", "arguments": {}}
                )
                data = host.get("result", {})
                state = data.get("structuredContent")
                if state is None:
                    try:
                        state = json.loads(
                            next(
                                b["text"]
                                for b in data.get("content", [])
                                if b.get("type") == "text"
                            )
                        )
                    except (ValueError, KeyError, StopIteration, TypeError):
                        state = {}
                resources = (
                    state.get("resources", {}) if isinstance(state, dict) else {}
                )
                light = name in {
                    "file_list",
                    "file_read",
                    "file_write",
                    "file_replace",
                    "file_stat",
                    "file_mkdir",
                    "file_copy",
                    "file_move",
                    "file_read_lines",
                    "file_read_chunk",
                    "file_upload",
                    "system_process_list",
                }
                if name == "file_upload" and arguments.get("action") == "commit":
                    light = False
                ready = (
                    resources.get("light_operations_ready")
                    if light
                    else resources.get("admission_ready")
                )
                if not ready or not -2 <= time.time() - resources.get("at", 0) <= 6:
                    raise RuntimeError(
                        "Machine busy or resource monitor unavailable; retry after 6 seconds"
                    )
        reply = backend.request("tools/call", params)
        if reply.get("error"):
            return {"_upstream_rpc_error": reply["error"]}
        result = normalized_output(public_name, reply.get("result", {}))
        if (
            name == "bridge_status"
            and isinstance(result, dict)
            and isinstance(result.get("content"), list)
        ):
            result["content"].append(
                {
                    "type": "text",
                    "text": f"UDB Unified {VERSION} | {len(self.tool_cache or [])} public tools | {len(self.tool_routes)} backend tools (legacy aliases retained)",
                }
            )
            if hasattr(self.primary, "status"):
                result["content"].append(
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "backend_health": {
                                    "original": self.primary.status(),
                                    "companion": self.companion.status(),
                                }
                            },
                            ensure_ascii=False,
                        ),
                    }
                )
        from .protocol import annotate

        return annotate(result)

    def handle(self, message: dict[str, Any]) -> None:
        ident = message.get("id")
        tool = None
        call_id = uuid.uuid4().hex[:8]
        started = time.monotonic()
        observed_result = None
        observed_error = None
        try:
            method = message.get("method")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Invalid MCP method parameters")
            if method == "initialize":
                result = self.initialize(params)
            elif method == "tools/list":
                if not self.startup_ok:
                    raise RuntimeError("MCP not initialized")
                result = self.tools_list()
            elif method == "tools/call":
                candidate = params.get("name")
                tool = candidate if isinstance(candidate, str) else "unknown"
                self.activity_event(
                    "call", tool, call_id, arguments=params.get("arguments")
                )
                if not self.startup_ok:
                    raise RuntimeError("MCP not initialized")
                result = self.call_tool(params)
                observed_result = result
                if "_upstream_rpc_error" in result:
                    self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": ident,
                            "error": result["_upstream_rpc_error"],
                        }
                    )
                    return
            elif method == "ping":
                result = {}
            else:
                # Keep all original UDB non-tool APIs functional.
                reply = self.primary.request(method, params)
                if reply.get("error"):
                    self.send({"jsonrpc": "2.0", "id": ident, "error": reply["error"]})
                    return
                result = reply.get("result", {})
            self.send({"jsonrpc": "2.0", "id": ident, "result": result})
        except TimeoutError as e:
            observed_error = e
            self.send(rpc_error(ident, str(e), -32002))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
            observed_error = e
            self.send(rpc_error(ident, str(e)))
        finally:
            if tool is not None:
                self.activity_event(
                    "result",
                    tool,
                    call_id,
                    result=observed_result,
                    error=observed_error,
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            self.active_limit.release()

    def serve(self) -> None:
        # A raw "for line in stdin" can accumulate arbitrarily large input
        # before checking limits. Use bounded reads and drain oversize frames.
        while True:
            wire = sys.stdin.buffer.readline(MAX_LINE + 1)
            if not wire:
                break
            if len(wire) > MAX_LINE:
                while wire and not wire.endswith(b"\n"):
                    wire = sys.stdin.buffer.readline(MAX_LINE + 1)
                self.send(rpc_error(None, "MCP request exceeds frame limit", -32700))
                continue
            try:
                message = json.loads(wire)
            except (ValueError, UnicodeError):
                self.send(rpc_error(None, "Invalid JSON-RPC request", -32700))
                continue
            if not isinstance(message, dict):
                self.send(rpc_error(None, "Invalid JSON-RPC object", -32600))
                continue
            ident = message.get("id")
            if (
                message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or isinstance(ident, bool)
                or not isinstance(ident, (str, int, type(None)))
            ):
                self.send(
                    rpc_error(
                        ident if type(ident) in (str, int) else None,
                        "Invalid JSON-RPC request",
                        -32600,
                    )
                )
                continue
            if "id" not in message:
                for backend in (self.primary, self.companion):
                    backend.notification(message)
                continue
            if not self.active_limit.acquire(timeout=10):
                self.send(rpc_error(message.get("id"), "UDB Unified is busy", -32003))
                continue
            threading.Thread(target=self.handle, args=(message,), daemon=True).start()


def run(settings) -> int:
    from .launcher import worker_command

    home = settings.home
    root = settings.project_root
    if not home.is_dir() or not root.is_dir():
        print(
            "UDB: existing home and configured project root are required",
            file=sys.stderr,
        )
        return 2
    if settings.core_backend == "installed":
        if not settings.core_exe or not settings.core_exe.is_file():
            raise ValueError("Configured installed core is missing")
        original_argv = [str(settings.core_exe), "--stdio", "--home", str(home)]
    else:
        original_argv = worker_command("core", ["--stdio", "--home", str(home)])
    companion_argv = worker_command(
        "companion",
        [
            "--root",
            str(root),
            "--udb-home",
            str(home),
            *(["--enable-terminal"] if settings.enable_terminal else []),
            "--parent-pid",
            str(os.getpid()),
        ],
    )
    proxy: UnifiedProxy | None = None
    original = RecoveringChild(
        lambda: StdioChild(
            "Original UDB",
            original_argv,
            str(home),
            lambda msg: proxy.send(msg) if proxy else None,
        ),
        "Original UDB",
    )
    companion = RecoveringChild(
        lambda: StdioChild(
            "Companion",
            companion_argv,
            str(home),
            lambda msg: proxy.send(msg) if proxy else None,
        ),
        "Companion",
    )
    from .activity import Activity

    proxy = UnifiedProxy(original, companion, Activity(home))
    try:
        original.start()
        companion.start()
        proxy.serve()
        return 0
    except (OSError, RuntimeError, ValueError) as e:
        print(f"UDB Unified: {type(e).__name__}", file=sys.stderr)
        return 1
    finally:
        proxy.closed.set()
        companion.close()
        original.close()


def main(argv=None):
    from .cli import worker

    return worker("legacy-proxy", sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    from .cli import main

    raise SystemExit(main(["serve", *sys.argv[1:]]))
