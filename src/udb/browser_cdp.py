"""Bounded CDP WebSocket request routing for an UDB-owned browser.

The host serializes actions; this channel additionally owns the socket lock and
keeps bounded, redacted event metadata. It never connects to a user profile.
Uncertain timed-out commands are never automatically replayed.
"""

from __future__ import annotations

import json
import math
import threading
import time
from collections import deque

from .core import PolicyError

MAX_REPLY_BYTES = 12 * 1048576
MAX_EVENT_MARKERS = 128
MAX_EVENT_QUEUE = 128
MAX_COMMAND_TIMEOUT_SECONDS = 60
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
        self._events = deque(maxlen=MAX_EVENT_QUEUE)

    def event_markers(self):
        """Return bounded event names only, without URL, cookie or body values."""
        with self._lock:
            return tuple(self._event_markers)

    def _record_event(self, message):
        method = message.get("method")
        if not isinstance(method, str) or len(method) > 96:
            return
        if method.startswith(_EVENT_PREFIXES):
            self._event_markers.append(method)
        params = message.get("params")
        if not isinstance(params, dict):
            return
        event = {"method": method}
        session = message.get("sessionId")
        if isinstance(session, str):
            event["sessionId"] = session
        if method == "Page.frameNavigated":
            frame = params.get("frame")
            if not isinstance(frame, dict):
                return
            for key in ("id", "loaderId", "parentId"):
                value = frame.get(key)
                if isinstance(value, str) and len(value) <= 128:
                    event["frameId" if key == "id" else key] = value
        elif method == "Page.navigatedWithinDocument":
            frame_id = params.get("frameId")
            nav_type = params.get("navigationType")
            if isinstance(frame_id, str) and len(frame_id) <= 128:
                event["frameId"] = frame_id
            if isinstance(nav_type, str) and len(nav_type) <= 32:
                event["navigationType"] = nav_type
        elif method == "Page.lifecycleEvent":
            for key in ("frameId", "loaderId", "name"):
                value = params.get(key)
                if isinstance(value, str) and len(value) <= 128:
                    event[key] = value
        elif method == "Target.targetDestroyed":
            target_id = params.get("targetId")
            if isinstance(target_id, str) and len(target_id) <= 128:
                event["targetId"] = target_id
        else:
            return
        self._events.append(event)

    def wait_for_event(self, methods, session=None, timeout=1, match=None):
        """Wait for a whitelisted event without retaining page URLs or payloads."""
        from websocket import (
            WebSocketConnectionClosedException,
            WebSocketTimeoutException,
        )

        wanted = (methods,) if isinstance(methods, str) else tuple(methods)
        if not wanted or any(
            not isinstance(name, str) or len(name) > 96 for name in wanted
        ):
            raise PolicyError("Invalid browser event name")
        if match is None:
            match = {}
        if not isinstance(match, dict) or any(
            not isinstance(key, str) or not isinstance(value, (str, int, bool))
            for key, value in match.items()
        ):
            raise PolicyError("Invalid browser event filter")
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= MAX_COMMAND_TIMEOUT_SECONDS
        ):
            raise PolicyError("Invalid browser command timeout")
        with self._lock:
            deadline = time.monotonic() + timeout
            while True:
                for event in tuple(self._events):
                    if (
                        event.get("method") in wanted
                        and (session is None or event.get("sessionId") == session)
                        and all(event.get(key) == value for key, value in match.items())
                    ):
                        self._events.remove(event)
                        return dict(event)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                if not self.process or not self.process.alive():
                    raise PolicyError("Owned browser has exited")
                self.websocket.settimeout(min(1, max(0.01, remaining)))
                try:
                    raw = self.websocket.recv()
                except WebSocketTimeoutException:
                    continue
                except (OSError, WebSocketConnectionClosedException) as exc:
                    raise PolicyError("Private browser connection closed") from exc
                if not raw:
                    raise PolicyError("Private browser connection closed")
                if isinstance(raw, str):
                    reply_size = len(raw.encode("utf-8"))
                elif isinstance(raw, (bytes, bytearray)):
                    reply_size = len(raw)
                else:
                    raise PolicyError("Invalid browser protocol reply")
                if reply_size > MAX_REPLY_BYTES:
                    raise PolicyError("Browser reply exceeds limit")
                try:
                    reply = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    raise PolicyError("Invalid browser protocol reply") from exc
                if not isinstance(reply, dict):
                    raise PolicyError("Invalid browser protocol reply")
                if isinstance(reply.get("method"), str):
                    self._record_event(reply)
                # A response left behind by an expired command is deliberately
                # ignored; this waiter never owns or replays that command.

    def call(self, method, params=None, session=None, timeout=10):
        from websocket import WebSocketConnectionClosedException, WebSocketTimeoutException

        with self._lock:
            if not self.process or not self.process.alive():
                raise PolicyError("Owned browser has exited")
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout)
                or not 0 < timeout <= MAX_COMMAND_TIMEOUT_SECONDS
            ):
                raise PolicyError("Invalid browser command timeout")
            self.seq += 1
            request_id = self.seq
            message = dict(id=request_id, method=method, params=params or {})
            if session:
                message["sessionId"] = session
            try:
                self.websocket.send(
                    json.dumps(message, separators=(",", ":"), ensure_ascii=False)
                )
            except (OSError, WebSocketConnectionClosedException) as exc:
                raise PolicyError("Private browser connection closed") from exc
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
                except (OSError, WebSocketConnectionClosedException) as exc:
                    raise PolicyError("Private browser connection closed") from exc
                if not raw:
                    raise PolicyError("Private browser connection closed")
                if isinstance(raw, str):
                    reply_size = len(raw.encode("utf-8"))
                elif isinstance(raw, (bytes, bytearray)):
                    reply_size = len(raw)
                else:
                    raise PolicyError("Invalid browser protocol reply")
                if reply_size > MAX_REPLY_BYTES:
                    raise PolicyError("Browser reply exceeds limit")
                try:
                    reply = json.loads(raw)
                except (ValueError, TypeError) as exc:
                    raise PolicyError("Invalid browser protocol reply") from exc
                if not isinstance(reply, dict):
                    raise PolicyError("Invalid browser protocol reply")
                if type(reply.get("id")) is not int or reply.get("id") != request_id:
                    if isinstance(reply.get("method"), str):
                        self._record_event(reply)
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
