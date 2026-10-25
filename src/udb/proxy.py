"""UDB Unified MCP proxy: existing UDB tool set + local UDB Companion.

No GUI or privilege changes. One stdio MCP endpoint. Python stdlib only.
Existing owner grants/state stay untouched; core and Companion share this package.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from typing import Any

from udb.catalog import public_catalog, resolve_call
from udb.recovery import RecoveringChild

from udb._version import VERSION
MAX_LINE = 16 * 1024 * 1024
MAX_WAIT = 90.0
CHILD_SHUTDOWN_WAIT = 3.0
MAX_CONCURRENT = 8


def compact_json(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def rpc_error(rpc_id: Any, message: str, code: int = -32000) -> dict[str, Any]:
    from .protocol import metadata
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message,"data":{"udb":metadata("ERROR")}}}


def normalized_output(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    """Classify tool completion conservatively; preserve the original backend result.

    Native tool errors must never be presented as PASS. A successful *tool call*
    does not imply an asynchronous job has finished.
    """
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    value = copy.deepcopy(result)
    content = value["content"]
    text_blocks = [i.get("text", "") for i in content
                   if isinstance(i, dict) and i.get("type") == "text"
                   and isinstance(i.get("text"), str)]
    first = text_blocks[0].strip() if text_blocks else ""
    first_line = first.splitlines()[0] if first else ""
    native = re.match(r"^(PASS|RUNNING|ERROR)\s*\|", first_line)
    if native:
        if native.group(1) == "ERROR":
            value["isError"] = True
        return value

    report = None
    if first:
        candidate = first
        for _ in range(3):
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError):
                break
            if isinstance(parsed, str):
                candidate = parsed
                continue
            if isinstance(parsed, dict):
                report = parsed
            break

    state = "PASS"
    label = "Tool returned"
    if value.get("isError") is True:
        state, label = "ERROR", "Backend reported a tool error"
    elif first_line.startswith((
        "Error executing tool", "Error calling MCP tool",
        "Access denied:", "Permission denied:",
        "Traceback (most recent call last)"
    )):
        state, label = "ERROR", "Backend returned an error message"
    elif isinstance(report, dict):
        status = str(report.get("status", report.get("state", ""))).lower()
        code = report.get("exit_code")
        result_flag = report.get("result")
        error_value = report.get("error")
        failed = (
            report.get("isError") is True or
            report.get("success") is False or
            report.get("ok") is False or
            report.get("failed") is True or
            (error_value not in (None, "", False, {}, [])) or
            (isinstance(result_flag, str) and result_flag.upper() in {"FAIL", "FAILED", "ERROR"})
        )
        if failed:
            state, label = "ERROR", "Backend reported failure"
        elif status in {"failed", "error", "denied", "rejected", "cancelled", "canceled"}:
            state, label = "ERROR", "Task failed"
        elif isinstance(code, int) and not isinstance(code, bool) and code != 0:
            state, label = "ERROR", "Process exit code is nonzero"
        elif status in {"running", "pending", "queued", "started", "waiting", "executing",
                        "needs_approval", "pending_approval", "awaiting_approval"}:
            state, label = "RUNNING", "Task pending or running"
        elif status in {"exited", "finished", "completed", "complete", "done"}:
            state, label = "PASS", "Process finished"
    value["content"] = [{"type": "text", "text": f"{state} | {label} ({tool})"}] + content
    if state == "ERROR":
        value["isError"] = True
    return value

class StdioChild:
    def __init__(self, name: str, argv: list[str], cwd: str, notify):
        self.name = name
        self.argv = argv
        self.cwd = cwd
        self.notify = notify
        self.process: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.seq = itertools.count(1)
        self.waiters: dict[int, queue.Queue] = {}
        self.terminated = threading.Event()
        self.stderr_tail: list[str] = []

    def start(self) -> None:
        from .launcher import child_environment
        env = child_environment()
        for key in list(env):
            if key in ("CONTROL_PLANE_API_KEY", "TUNNEL_CLIENT_PROFILE_DIR", "_MEIPASS2") or key.startswith('_PYI_'):
                env.pop(key, None)
        env['PYINSTALLER_RESET_ENVIRONMENT']='1'
        self.process = subprocess.Popen(
            self.argv, env=env, cwd=self.cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, shell=False, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.read_thread=threading.Thread(target=self._read_loop, daemon=True, name=f"{self.name}-rx")
        self.error_thread=threading.Thread(target=self._stderr_loop, daemon=True, name=f"{self.name}-err")
        self.read_thread.start();self.error_thread.start()

    def _read_loop(self) -> None:
        try:
            assert self.process and self.process.stdout
            while True:
                line = self.process.stdout.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE or not line.endswith(b"\n"):
                    raise ValueError("Oversized or malformed backend MCP line")
                try:
                    message = json.loads(line)
                except (UnicodeError, json.JSONDecodeError):
                    raise ValueError("Backend did not produce JSON-RPC") from None
                if not isinstance(message, dict):
                    continue
                if "id" in message:
                    with self.lock:
                        waiter = self.waiters.pop(message["id"], None)
                    if waiter:
                        waiter.put(message)
                elif "method" in message:
                    # Never forward a child request unsolicited; only notifications.
                    if message.get("method", "").startswith("notifications/"):
                        self.notify(message)
        except (OSError, ValueError) as e:
            self.stderr_tail.append(type(e).__name__)
        finally:
            self.terminated.set()
            with self.lock:
                active, self.waiters = self.waiters, {}
            for waiter in active.values():
                waiter.put(None)

    def _stderr_loop(self) -> None:
        try:
            assert self.process and self.process.stderr
            # Drain stderr to avoid blocking; never reveal arbitrary backend output.
            for _ in iter(lambda: self.process.stderr.readline(4096), b""):
                pass
        except OSError:
            pass

    def _send(self, message: dict[str, Any]) -> None:
        if self.terminated.is_set() or not self.process or not self.process.stdin:
            raise RuntimeError(f"{self.name} MCP backend disconnected")
        wire = compact_json(message)
        if len(wire) > MAX_LINE:
            raise ValueError("MCP request exceeds maximum length")
        with self.write_lock:
            try:
                self.process.stdin.write(wire)
                self.process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as e:
                raise RuntimeError(f"{self.name} MCP backend write failed") from e

    def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = MAX_WAIT) -> dict[str, Any]:
        ident = next(self.seq)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            self.waiters[ident] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params or {}})
            try:
                result = waiter.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"{self.name} MCP response timed out; outcome unknown") from None
            if result is None:
                raise RuntimeError(f"{self.name} MCP backend disconnected")
            return result
        finally:
            with self.lock:
                self.waiters.pop(ident, None)

    def notification(self, message: dict[str, Any]) -> None:
        try:
            self._send(message)
        except (OSError, ValueError, RuntimeError):
            pass

    def close(self) -> None:
        if not self.process:
            return
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            try:
                self.process.wait(CHILD_SHUTDOWN_WAIT)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(CHILD_SHUTDOWN_WAIT)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(CHILD_SHUTDOWN_WAIT)
        for name in ('read_thread','error_thread'):
            thread=getattr(self,name,None)
            if thread and thread is not threading.current_thread():thread.join(timeout=1)
        for stream in (self.process.stdout,self.process.stderr):
            if stream:
                try:stream.close()
                except OSError:pass


class UnifiedProxy:
    def __init__(self, primary: StdioChild, companion: StdioChild):
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

    def send(self, message: dict[str, Any]) -> None:
        with self.send_lock:
            data = compact_json(message)
            if len(data) > MAX_LINE:
                # Never silently discard a response: the caller would hang and
                # might retry an already-executed, side-effecting operation.
                if "id" not in message:
                    return  # oversized notification has no response contract
                data = compact_json(rpc_error(
                    message["id"],
                    "MCP response exceeds size limit; do not replay side effects",
                    -32005,
                ))
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
                threading.Thread(target=self._watch_access, daemon=True, name="UDB grant watcher").start()
            self.startup_ok = True
            return base

    def _watch_access(self):
        while not self.closed.wait(1):
            try:
                reply = self.primary.request("tools/call", {"name": "computer_status", "arguments": {}}, timeout=5)
                result = reply.get("result", {})
                grant = result.get("structuredContent")
                if grant is None:
                    grant = json.loads(next(b["text"] for b in result.get("content", []) if b.get("type") == "text"))
                if result.get("isError") or not isinstance(grant, dict) or grant.get("enabled") is not True:
                    self.companion.request("udb/close-sessions", timeout=10)
                elif 'desktop' not in grant.get('capabilities',[]):
                    self.companion.request('udb/close-desktop',timeout=10)
            except (ValueError, KeyError, StopIteration, TypeError, RuntimeError, TimeoutError, OSError):
                try:
                    self.companion.request("udb/close-sessions", timeout=10)
                except (RuntimeError, TimeoutError, OSError):
                    return

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
        if name not in self.tool_routes and name not in {t['name'] for t in self.tool_cache or []}:
            raise ValueError("Unknown MCP tool")
        name, arguments = resolve_call(name, arguments)
        params = {**params, "name": name, "arguments": arguments}
        backend = self.tool_routes.get(name)
        if backend is None:
            raise ValueError("Unknown MCP tool")
        if backend is self.companion:
            access = self.primary.request("tools/call", {"name": "computer_status", "arguments": {}})
            result = access.get("result", {})
            grant = result.get("structuredContent")
            if grant is None:
                try:
                    grant = json.loads(next(b["text"] for b in result.get("content", []) if b.get("type") == "text"))
                except (ValueError, KeyError, StopIteration, TypeError):
                    grant = {}
            if result.get("isError") or not isinstance(grant, dict) or grant.get("enabled") is not True:
                self.companion.request("udb/close-sessions", timeout=10)
                raise RuntimeError("Existing UDB owner computer access is required; AI cannot grant it")
            arguments=params.get('arguments', {})
            if name=='virtual_pointer' and 'desktop' not in grant.get('capabilities',[]):raise RuntimeError('Existing desktop capability is required')
            if name=='terminal_start' and arguments.get('mode')=='conpty' and 'execute' not in grant.get('capabilities',[]):raise RuntimeError('Existing execute capability is required')
            job_control = (name == 'file_operation' and arguments.get('action') in ('read','list','cancel')) or (name == 'terminal_interact' and not arguments.get('text')) or (name=='virtual_pointer' and arguments.get('action') in ('hide','stop','status')) or (name=='work_recovery' and arguments.get('action','list')!='resume')
            if not job_control and name not in {"terminal_stop", "terminal_read", "terminal_list", "terminal_wait", "search_read", "search_stop", "system_process_terminate"}:
                host = self.primary.request("tools/call", {"name": "host_status", "arguments": {}})
                data = host.get("result", {})
                state = data.get("structuredContent")
                if state is None:
                    try:
                        state = json.loads(next(b["text"] for b in data.get("content", []) if b.get("type") == "text"))
                    except (ValueError, KeyError, StopIteration, TypeError):
                        state = {}
                resources = state.get("resources", {}) if isinstance(state, dict) else {}
                light = name in {"file_list", "file_read", "file_write", "file_replace", "file_stat",
                                 "file_mkdir", "file_copy", "file_move", "file_read_lines", "file_read_chunk", "file_upload", "system_process_list"}
                if name == "file_upload" and arguments.get("action") == "commit":light=False
                ready = resources.get("light_operations_ready") if light else resources.get("admission_ready")
                if (not ready or not -2 <= time.time() - resources.get("at", 0) <= 6):
                    raise RuntimeError("Machine busy or resource monitor unavailable; retry after 6 seconds")
        reply = backend.request("tools/call", params)
        if reply.get("error"):
            return {"_upstream_rpc_error": reply["error"]}
        result=normalized_output(public_name, reply.get("result", {}))
        if name=='bridge_status' and isinstance(result,dict) and isinstance(result.get('content'),list):
            result['content'].append({'type':'text','text':f'UDB Unified {VERSION} | {len(self.tool_cache or [])} public tools | {len(self.tool_routes)} backend tools (legacy aliases retained)'})
            if hasattr(self.primary,'status'):
                result['content'].append({'type':'text','text':json.dumps({'backend_health':{'original':self.primary.status(),'companion':self.companion.status()}},ensure_ascii=False)})
        from .protocol import annotate
        return annotate(result)

    def handle(self, message: dict[str, Any]) -> None:
        ident = message.get("id")
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
                if not self.startup_ok:
                    raise RuntimeError("MCP not initialized")
                result = self.call_tool(params)
                if "_upstream_rpc_error" in result:
                    self.send({"jsonrpc": "2.0", "id": ident, "error": result["_upstream_rpc_error"]})
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
            self.send(rpc_error(ident, str(e), -32002))
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as e:
            self.send(rpc_error(ident, str(e)))
        finally:
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
            ident=message.get('id')
            if (message.get('jsonrpc')!='2.0' or not isinstance(message.get('method'),str) or
                    isinstance(ident,bool) or not isinstance(ident,(str,int,type(None)))):
                self.send(rpc_error(ident if type(ident) in (str,int) else None,'Invalid JSON-RPC request',-32600))
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
    from .launcher import child_environment, worker_command
    home=settings.home;root=settings.project_root
    if not home.is_dir() or not root.is_dir():
        print('UDB: existing home and configured project root are required',file=sys.stderr);return 2
    if settings.core_backend=='installed':
        if not settings.core_exe or not settings.core_exe.is_file():raise ValueError('Configured installed core is missing')
        original_argv=[str(settings.core_exe),'--stdio','--home',str(home)]
    else:original_argv=worker_command('core',['--stdio','--home',str(home)])
    companion_argv=worker_command('companion',['--root',str(root),'--udb-home',str(home),*(['--enable-terminal'] if settings.enable_terminal else []),'--parent-pid',str(os.getpid())])
    proxy: UnifiedProxy | None = None
    original = RecoveringChild(lambda: StdioChild("Original UDB", original_argv, str(home), lambda msg: proxy.send(msg) if proxy else None), 'Original UDB')
    companion = RecoveringChild(lambda: StdioChild("Companion", companion_argv, str(home), lambda msg: proxy.send(msg) if proxy else None), 'Companion')
    proxy = UnifiedProxy(original, companion)
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
    return worker('legacy-proxy',sys.argv[1:] if argv is None else argv)


if __name__ == "__main__":
    from .cli import main
    raise SystemExit(main(['serve',*sys.argv[1:]]))

