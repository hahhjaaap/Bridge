"""Single-flight MCP child recovery; never replay a request after dispatch."""

from __future__ import annotations

import copy
import json
import threading
import time
from typing import Any, Callable


class RecoveringChild:
    """Manage one MCP child and recover it only before a subsequent request."""

    def __init__(self, factory: Callable[[], Any], name: str) -> None:
        self.factory = factory
        self.name = name
        self.child = factory()
        self.lock = threading.RLock()
        self.closed = False
        self.initialization: dict[str, Any] | None = None
        self.schema: str | None = None
        self.generation = 0
        self.attempts: list[float] = []
        self.next_attempt = 0.0
        self.last_error = ""

    @property
    def process(self):
        return self.child.process

    def start(self) -> None:
        with self.lock:
            if self.closed:
                raise RuntimeError("MCP child is closed")
            self.child.start()
            self.generation = 1

    def _alive(self) -> bool:
        process = self.child.process
        return (
            process is not None
            and process.poll() is None
            and not self.child.terminated.is_set()
        )

    def _ready(self):
        with self.lock:
            if self.closed:
                raise RuntimeError("MCP child is closed")
            if self._alive():
                return self.child
            if self.initialization is None:
                raise RuntimeError(f"{self.name} MCP backend not initialized")

            now = time.monotonic()
            self.attempts = [stamp for stamp in self.attempts if now - stamp < 60]
            if now < self.next_attempt or len(self.attempts) >= 3:
                raise RuntimeError(
                    f"{self.name} backend recovery is cooling down; no request was sent"
                )

            self.attempts.append(now)
            replacement = self.factory()
            try:
                replacement.start()
                reply = replacement.request(
                    "initialize", copy.deepcopy(self.initialization), timeout=20
                )
                if reply.get("error"):
                    raise RuntimeError("Recovery initialization rejected")

                replacement.notification(
                    {"jsonrpc": "2.0", "method": "notifications/initialized"}
                )
                listing = replacement.request("tools/list", timeout=20)
                tools = listing.get("result", {}).get("tools")
                if listing.get("error") or not isinstance(tools, list):
                    raise RuntimeError("Recovery tool discovery failed")

                fingerprint = json.dumps(tools, sort_keys=True, separators=(",", ":"))
                if self.schema is not None and fingerprint != self.schema:
                    raise RuntimeError(
                        "Backend tool schema changed; refresh plugin before continuing"
                    )
            except BaseException as exc:
                replacement.close()
                self.last_error = type(exc).__name__
                self.next_attempt = time.monotonic() + min(8, 2 ** len(self.attempts))
                raise

            previous_child = self.child
            self.child = replacement
            self.generation += 1
            self.last_error = ""
            self.next_attempt = 0.0
            previous_child.close()
            return replacement

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        timeout: float = 90,
    ) -> dict[str, Any]:
        if method == "initialize":
            with self.lock:
                if self.closed:
                    raise RuntimeError("MCP child is closed")
                reply = self.child.request(method, params, timeout=timeout)
                if not reply.get("error"):
                    self.initialization = copy.deepcopy(params or {})
                return reply

        # Cleanup must not resurrect a dead Companion during revocation/shutdown.
        if method in ("udb/close-sessions", "udb/close-desktop"):
            with self.lock:
                if self.closed or not self._alive():
                    return {"result": {"closed": True, "sessions_lost": True}}
                child = self.child
        else:
            child = self._ready()

        # A dispatched request may already have caused side effects. Never replay it.
        # Only a later request may trigger backend recovery.
        reply = child.request(method, params, timeout=timeout)
        if method == "tools/list" and not reply.get("error"):
            tools = reply.get("result", {}).get("tools")
            fingerprint = json.dumps(tools, sort_keys=True, separators=(",", ":"))
            with self.lock:
                if child is self.child:
                    self.schema = fingerprint
        return reply

    def notification(self, message: dict[str, Any]) -> None:
        with self.lock:
            if not self.closed and self._alive():
                self.child.notification(message)

    def status(self) -> dict[str, Any]:
        with self.lock:
            now = time.monotonic()
            return {
                "alive": not self.closed and self._alive(),
                "generation": self.generation,
                "recovery_attempts_last_minute": sum(
                    now - stamp < 60 for stamp in self.attempts
                ),
                "last_error_kind": self.last_error,
                "replay_enabled": False,
            }

    def close(self) -> None:
        with self.lock:
            self.closed = True
            child = self.child
        child.close()
