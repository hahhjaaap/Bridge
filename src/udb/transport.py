"""JSON-RPC stdio transport for UDB backend worker processes."""

from __future__ import annotations

import itertools
import json
import queue
import subprocess
import threading
from typing import Any

from .pipe_io import write_all

MAX_LINE = 16 * 1024 * 1024


MAX_WAIT = 90.0


CHILD_SHUTDOWN_WAIT = 3.0


def compact_json(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def rpc_error(rpc_id: Any, message: str, code: int = -32000) -> dict[str, Any]:
    from .protocol import metadata

    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": code, "message": message, "data": {"udb": metadata("ERROR")}},
    }


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
            if key in (
                "CONTROL_PLANE_API_KEY",
                "TUNNEL_CLIENT_PROFILE_DIR",
                "_MEIPASS2",
            ) or key.startswith("_PYI_"):
                env.pop(key, None)
        env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        self.process = subprocess.Popen(
            self.argv,
            env=env,
            cwd=self.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.read_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"{self.name}-rx"
        )
        self.error_thread = threading.Thread(
            target=self._stderr_loop, daemon=True, name=f"{self.name}-err"
        )
        self.read_thread.start()
        self.error_thread.start()

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
                    # UDB sends integer request IDs. A malformed child reply
                    # must not terminate the reader or strand other callers.
                    ident = message["id"]
                    if type(ident) is not int:
                        continue
                    with self.lock:
                        waiter = self.waiters.pop(ident, None)
                    if waiter:
                        waiter.put(message)
                elif "method" in message:
                    # Only string-valued MCP notifications may be forwarded.
                    # Reject malformed frames without losing live RPC replies.
                    method = message["method"]
                    if isinstance(method, str) and method.startswith(
                        "notifications/"
                    ):
                        try:
                            self.notify(message)
                        except Exception:
                            # Notification delivery is best-effort. A failing
                            # observer must not disconnect the backend or
                            # discard replies to unrelated in-flight calls.
                            pass
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
                write_all(self.process.stdin, wire)
                self.process.stdin.flush()
            except (OSError, ValueError, BrokenPipeError) as e:
                raise RuntimeError(f"{self.name} MCP backend write failed") from e

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = MAX_WAIT,
    ) -> dict[str, Any]:
        ident = next(self.seq)
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self.lock:
            self.waiters[ident] = waiter
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": ident,
                    "method": method,
                    "params": params or {},
                }
            )
            try:
                result = waiter.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(
                    f"{self.name} MCP response timed out; outcome unknown"
                ) from None
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
        for name in ("read_thread", "error_thread"):
            thread = getattr(self, name, None)
            if thread and thread is not threading.current_thread():
                thread.join(timeout=1)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                try:
                    stream.close()
                except OSError:
                    pass
