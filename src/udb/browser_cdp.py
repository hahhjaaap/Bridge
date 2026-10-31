"""Bounded CDP WebSocket request routing for an UDB-owned browser.

The host serializes actions; this channel additionally owns the socket lock and
keeps bounded, redacted event metadata. It never connects to a user profile.
Uncertain timed-out commands are never automatically replayed.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque

from .core import PolicyError

MAX_REPLY_BYTES = 12 * 1048576
MAX_EVENT_MARKERS = 128
_EVENT_PREFIXES = ("Target.", "Page.", "Runtime.", "Network.")


class CDPChannel:
    """One browser-owned WebSocket; serialize requests and preserve safe events."""

    def __init__(self, websocket, process):
        self.websocket = websocket
        self.process = process
        self.seq = 0
        self._lock = threading.RLock()
        # Never retain raw CDP event params: they can contain URLs and secrets.
        self._event_markers = deque(maxlen=MAX_EVENT_MARKERS)

    def event_markers(self):
        """Return bounded event names only, without URL, cookie or body values."""
        with self._lock:
            return tuple(self._event_markers)

    def call(self, method, params=None, session=None, timeout=10):
        from websocket import WebSocketTimeoutException

        with self._lock:
            if not self.process or not self.process.alive():
                raise PolicyError("Owned browser has exited")
            if timeout <= 0:
                raise PolicyError(
                    "Browser action budget exceeded; inspect before retrying"
                )
            self.seq += 1
            request_id = self.seq
            message = dict(id=request_id, method=method, params=params or {})
            if session:
                message["sessionId"] = session
            self.websocket.send(json.dumps(message))
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if not self.process.alive():
                    raise PolicyError("Owned browser was stopped")
                self.websocket.settimeout(
                    min(1, max(0.01, deadline - time.monotonic()))
                )
                try:
                    raw = self.websocket.recv()
                except WebSocketTimeoutException:
                    continue
                if not raw:
                    raise PolicyError("Private browser connection closed")
                if len(raw) > MAX_REPLY_BYTES:
                    raise PolicyError("Browser reply exceeds limit")
                try:
                    reply = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    raise PolicyError("Invalid browser protocol reply") from exc
                if not isinstance(reply, dict):
                    raise PolicyError("Invalid browser protocol reply")
                if reply.get("id") != request_id:
                    name = reply.get("method")
                    if (
                        isinstance(name, str)
                        and len(name) <= 96
                        and name.startswith(_EVENT_PREFIXES)
                    ):
                        self._event_markers.append(name)
                    # Late replies to timed-out calls must not satisfy a new
                    # request; never assume their side effects did not occur.
                    continue
                if "error" in reply:
                    error = reply["error"]
                    detail = (
                        error.get("message", "unknown")
                        if isinstance(error, dict)
                        else "unknown"
                    )
                    raise PolicyError("Browser operation failed: " + str(detail)[:300])
                return reply.get("result", {})
            raise PolicyError(
                "Browser operation timed out; outcome may be unknown, inspect before retrying"
            )
