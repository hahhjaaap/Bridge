"""Companion orchestration and stdio MCP entry point.

File policy, terminal I/O and tool schemas live in independent modules.
Legacy imports continue to resolve to the canonical classes and constants.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from udb._version import VERSION
from udb.companion.processes import ManagedProcess, watch_parent

from .constants import (
    HIDDEN_DIRS,
    HIDDEN_DIRS_CASEFOLD,
    MAX_BUFFER_BYTES,
    MAX_FILE_BYTES,
    MAX_OUTPUT_CHARS,
    MAX_READ_BYTES,
    MAX_RETAINED_SESSIONS,
    MAX_SCAN_FILES,
    MAX_SESSIONS,
    MAX_TERMINAL_OUTPUT_BYTES,
    PROTECTED_NAMES,
    PROTECTED_PREFIXES,
    PROTECTED_SUFFIXES,
)
from .dispatch import dispatch_tool
from .features import Features
from .files import ScopedFiles as ScopedFiles
from .files import digest as digest
from .files import is_reparse as is_reparse
from .responses import respond_text as respond_text
from .schemas import TOOLS as TOOLS
from .terminal import TerminalSession as TerminalSession
from .terminal import retained_tail

__all__ = [
    "TOOLS",
    "VERSION",
    "HIDDEN_DIRS",
    "HIDDEN_DIRS_CASEFOLD",
    "MAX_BUFFER_BYTES",
    "MAX_FILE_BYTES",
    "MAX_OUTPUT_CHARS",
    "MAX_READ_BYTES",
    "MAX_RETAINED_SESSIONS",
    "MAX_SCAN_FILES",
    "MAX_SESSIONS",
    "MAX_TERMINAL_OUTPUT_BYTES",
    "PROTECTED_NAMES",
    "PROTECTED_PREFIXES",
    "PROTECTED_SUFFIXES",
    "Runtime",
    "ScopedFiles",
    "TerminalSession",
    "digest",
    "is_reparse",
    "main",
    "respond_text",
    "selftest",
    "serve",
]


class Runtime:
    def __init__(self, root: str, enable_terminal: bool = False, bridge_home=None):
        self.files = ScopedFiles(root)
        self.bridge_home = Path(bridge_home).resolve() if bridge_home else None
        from udb.concurrency import TaskPool

        self.capacity = TaskPool(
            self.bridge_home / "state/task-slots"
            if self.bridge_home
            else self.files.root / ".udb-work-state/task-slots"
        )
        self.enable_terminal = enable_terminal
        self.sessions: dict[str, TerminalSession] = {}
        self.lock = threading.RLock()
        self.done = threading.Event()
        self.monitor_errors = 0
        self.features = Features(self)
        self.journal = self.features.background.journal
        self.last_checkpoint = 0
        self._terminal_checkpoint_signatures: dict[str, tuple] = {}
        self.monitor = threading.Thread(
            target=self._watch, daemon=True, name="UDB terminal deadlines"
        )
        self.monitor.start()

    def _watch(self):
        while not self.done.wait(0.1):
            try:
                self.features.background.expire()
            except (OSError, RuntimeError, ValueError):
                self.monitor_errors += 1
            with self.lock:
                if self.done.is_set():
                    return
                for session in list(self.sessions.values()):
                    try:
                        code = session.process.poll()
                        if code is not None:
                            session.finish_io()
                        if code is None and time.monotonic() > session.deadline:
                            session.stop("timed_out")
                    except (OSError, RuntimeError, ValueError):
                        self.monitor_errors += 1
                        try:
                            session.stop("monitor_error")
                        except (OSError, RuntimeError, ValueError):
                            pass
                if time.monotonic() - self.last_checkpoint > 0.5:
                    try:
                        self.checkpoint()
                    except (OSError, ValueError):
                        self.monitor_errors += 1
                    self.last_checkpoint = time.monotonic()

    def checkpoint(self):
        self.features.background.checkpoint()
        for stale_id in set(self._terminal_checkpoint_signatures).difference(
            self.sessions
        ):
            self._terminal_checkpoint_signatures.pop(stale_id, None)
        for session in list(self.sessions.values()):
            with session.lock:
                code = session.process.poll()
                signature = (
                    session.position,
                    len(session.chunks),
                    session.done,
                    code,
                    session.reason,
                    session.mode,
                )
                if self._terminal_checkpoint_signatures.get(session.id) == signature:
                    continue
                chunks = retained_tail(session.chunks)
                row = dict(
                    session_id=session.id,
                    state="finished"
                    if session.done and code is not None
                    else "interrupted",
                    exit_code=code,
                    output_base64=base64.b64encode(chunks).decode(),
                    earliest_offset=session.position - len(chunks),
                    next_offset=session.position,
                    terminal_mode=session.mode,
                    process_restorable=False,
                    replay_safe=False,
                )
            self.journal.save("terminal-" + session.id, row)
            self._terminal_checkpoint_signatures[session.id] = signature

    def close(self):
        self.done.set()
        try:
            self.shutdown()
        finally:
            if self.monitor is not threading.current_thread():
                self.monitor.join(timeout=3)

    def start(
        self,
        program: str,
        arguments: list[str] | None = None,
        cwd: str = ".",
        wait_ms: int = 150,
        timeout_seconds: int = 3600,
        memory_mib: int = 1024,
        mode="pipe",
        columns=100,
        rows=30,
    ) -> dict[str, Any]:
        with self.lock:
            if self.done.is_set():
                raise RuntimeError("Companion is closed; no process was started")
            session, persistence_available = self._start(
                program,
                arguments,
                cwd,
                timeout_seconds,
                memory_mib,
                mode,
                columns,
                rows,
            )
        # Waiting for output must not hold the admission/shutdown lock.
        row = session.read(offset=0, wait_ms=wait_ms)
        row["persistence_available"] = persistence_available
        return row

    def _start(
        self, program, arguments, cwd, timeout_seconds, memory_mib, mode, columns, rows
    ):
        if not self.enable_terminal:
            raise PermissionError(
                "Terminal disabled. Owner must restart with --enable-terminal"
            )
        active = sum(
            session.process.poll() is None for session in self.sessions.values()
        )
        if active >= MAX_SESSIONS:
            raise RuntimeError("Maximum 10 concurrent processes; stop one first")
        # Keep a bounded history. Evict only finished sessions; never detach
        # a still-running process or replay an earlier command.
        if len(self.sessions) >= MAX_RETAINED_SESSIONS:
            for session_id in list(self.sessions):
                if self.sessions[session_id].process.poll() is not None:
                    self.sessions[session_id].stop("retired")
                    del self.sessions[session_id]
                    self._terminal_checkpoint_signatures.pop(session_id, None)
                    if len(self.sessions) < MAX_RETAINED_SESSIONS:
                        break
        if len(self.sessions) >= MAX_RETAINED_SESSIONS:
            raise RuntimeError("Session history full; running processes were kept")
        location = self.files.path(cwd)
        if not location.is_dir():
            raise ValueError("cwd is not a directory")
        if (
            not isinstance(program, str)
            or not program.strip()
            or not isinstance(arguments or [], list)
        ):
            raise ValueError("Invalid program/arguments")
        argv = [program] + (arguments or [])
        if any(not isinstance(arg, str) for arg in argv):
            raise ValueError("Arguments must be strings")
        if (
            not isinstance(timeout_seconds, int)
            or not 1 <= timeout_seconds <= 3600
            or not isinstance(memory_mib, int)
            or not 128 <= memory_mib <= 4096
        ):
            raise ValueError("Terminal timeout 1..3600s, memory 128..4096 MiB")
        executable = (
            shutil.which(program) if not Path(program).is_absolute() else program
        )
        if not executable or not Path(executable).is_file():
            raise ValueError("Program was not found")
        argv[0] = str(Path(executable).resolve())
        if (
            len(argv) > 129
            or any("\0" in a for a in argv)
            or sum(len(a) for a in argv) > 24000
        ):
            raise ValueError("Bounded program arguments required")
        if mode not in ("pipe", "conpty"):
            raise ValueError("Terminal mode pipe or conpty")
        slot = self.capacity.claim()
        process = None
        try:
            process = ManagedProcess(
                argv,
                str(location),
                memory_mib,
                conpty=mode == "conpty",
                columns=columns,
                rows=rows,
            )
            session = TerminalSession(process, argv)
            session.capacity = slot
        except BaseException:
            if process is not None:
                process.close()
            slot.close()
            raise
        session.mode = mode
        session.deadline = time.monotonic() + timeout_seconds
        self.sessions[session.id] = session
        persistence_available = True
        try:
            self.checkpoint()
        except (OSError, ValueError):
            self.monitor_errors += 1
            persistence_available = False
        return session, persistence_available

    def shutdown(self) -> None:
        """Stop all child processes owned by this Companion when MCP closes."""
        with self.lock:
            self._shutdown()

    def _shutdown(self):
        for session in list(self.sessions.values()):
            try:
                session.stop("shutdown")
            except (OSError, RuntimeError, subprocess.SubprocessError):
                pass
        self.features.close()
        self.checkpoint()

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch through the canonical companion router."""
        return dispatch_tool(self, name, args)


def serve(rt: Runtime) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

    def send(row):
        sys.stdout.write(json.dumps(row, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    while True:
        line = sys.stdin.buffer.readline(16 * 1024 * 1024 + 1)
        if not line:
            break
        ident = None
        try:
            if len(line) > 16 * 1024 * 1024:
                while line and not line.endswith(b"\n"):
                    line = sys.stdin.buffer.readline(65536)
                raise ValueError("Frame limit exceeded")
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("JSON-RPC object required")
            if "id" not in message:
                continue
            ident = message["id"]
            if isinstance(ident, bool) or not isinstance(ident, (str, int, type(None))):
                raise ValueError("Invalid request ID")
            method = message.get("method")
            params = message.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Object parameters required")
            if method == "initialize":
                value = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "UDB Companion v2", "version": VERSION},
                }
            elif method == "ping":
                value = {}
            elif method == "udb/close-sessions":
                rt.shutdown()
                value = {}
            elif method == "udb/close-desktop":
                if hasattr(rt.features, "pointer"):
                    rt.features.pointer.close()
                    del rt.features.pointer
                value = {}
            elif method == "tools/list":
                value = {"tools": TOOLS}
            elif method == "tools/call":
                args = params.get("arguments") or {}
                if not isinstance(args, dict) or not isinstance(
                    params.get("name"), str
                ):
                    raise ValueError("Tool name/object arguments required")
                try:
                    value = rt.tool(params["name"], args)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    RuntimeError,
                    UnicodeError,
                    TypeError,
                    AttributeError,
                    OverflowError,
                    subprocess.SubprocessError,
                ) as exc:
                    value = respond_text("ERROR", type(exc).__name__ + ": " + str(exc))
            else:
                send(
                    dict(
                        jsonrpc="2.0",
                        id=ident,
                        error=dict(code=-32601, message="Method not found"),
                    )
                )
                continue
            send(dict(jsonrpc="2.0", id=ident, result=value))
        except (json.JSONDecodeError, UnicodeDecodeError):
            send(
                dict(
                    jsonrpc="2.0",
                    id=None,
                    error=dict(code=-32700, message="Invalid JSON-RPC JSON"),
                )
            )
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            send(
                dict(jsonrpc="2.0", id=ident, error=dict(code=-32602, message=str(exc)))
            )


def selftest(root: str) -> int:
    rt = Runtime(root, enable_terminal=True)
    filename = f".udb-companion-test-{uuid.uuid4().hex[:8]}.txt"
    try:
        create = rt.files.write(filename, "line one\n", "NEW")
        assert create["created"]
        found = rt.files.search(".", filename)
        assert filename in found["matches"]
        viewed = rt.files.read(filename)
        assert viewed["content"] == "line one\n"
        rt.files.replace(filename, "one", "two", viewed["sha256"])
        assert rt.files.read(filename)["content"] == "line two\n"
        print("PASS | File create/search/read/edit", flush=True)
        # A managed terminal with normal command completion.
        task = rt.start(sys.executable, ["-u", "-c", "print('hello-UDB')"], ".", 300)
        ident = task["session_id"]
        cursor = task["next_offset"]
        chunks = [task["output"]]
        for _ in range(20):
            read = rt.sessions[ident].read(cursor, 200)
            chunks.append(read["output"])
            cursor = read["next_offset"]
            if read["state"] != "RUNNING" and not read["has_more"]:
                break
        assert "hello-UDB" in "".join(chunks)
        assert read["state"] == "PASS"
        print("PASS | Terminal execute/read/completion", flush=True)
        # Interactive REPL sends data to one process without launching a new one.
        task = rt.start(sys.executable, ["-u", "-i"], ".", 150)
        ident = task["session_id"]
        rt.sessions[ident].send("print('interactive-ok')\n")
        cursor = task["next_offset"]
        data = task["output"]
        for _ in range(20):
            row = rt.sessions[ident].read(cursor, 150)
            data += row["output"]
            cursor = row["next_offset"]
            if "interactive-ok" in data:
                break
        assert "interactive-ok" in data
        rt.sessions[ident].stop()
        print("PASS | Terminal interactive input/stop", flush=True)
        try:
            rt.files.read("../outside.txt")
            raise AssertionError("Escaped root")
        except PermissionError:
            print("PASS | Root scope guard", flush=True)
        print("PASS | Self-test complete", flush=True)
        return 0
    except Exception as e:
        print(f"ERROR | Self-test: {type(e).__name__}: {e}", flush=True)
        return 1
    finally:
        p = rt.files.path(filename, allow_missing=True)
        p.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent text-first MCP companion for UDB"
    )
    parser.add_argument(
        "--root", required=True, help="Existing approved project folder"
    )
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--udb-home", help=argparse.SUPPRESS)
    parser.add_argument(
        "--enable-terminal",
        action="store_true",
        help="Owner opt-in for subprocess execution",
    )
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest(args.root)
    runtime = Runtime(
        args.root, enable_terminal=args.enable_terminal, bridge_home=args.udb_home
    )

    def parent_gone():
        try:
            runtime.close()
        finally:
            os._exit(0)

    watch_parent(args.parent_pid, parent_gone)
    try:
        serve(runtime)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
