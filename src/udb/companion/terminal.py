"""Owned terminal I/O and bounded output buffering, independent of MCP."""

from __future__ import annotations

import codecs
import collections
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from typing import Any

from udb.pipe_io import write_all

from .constants import MAX_BUFFER_BYTES, MAX_TERMINAL_OUTPUT_BYTES


def retained_tail(chunks, limit=65536):
    """Copy only the checkpoint tail; callers hold the session's buffer lock."""
    parts = []
    remaining = limit
    for _, piece in reversed(chunks):
        if remaining <= 0:
            break
        part = piece[-remaining:]
        parts.append(part)
        remaining -= len(part)
    return b"".join(reversed(parts))


class TerminalSession:
    def __init__(self, process: subprocess.Popen[bytes], command: list[str]):
        self.id = uuid.uuid4().hex[:12]
        self.process = process
        self.capacity = None
        self.command = command
        self.chunks: collections.deque[tuple[int, bytes]] = collections.deque()
        self.position = 0
        self.read_cursor = 0
        self.lock = threading.Lock()
        self.arrival = threading.Event()
        self.done = False
        self.tail = b""
        self.last_output = 0
        self.reason = ""
        self.deadline = time.monotonic() + 3600
        self.mode = "pipe"
        self.input_queue = queue.Queue(maxsize=4)
        self.input_done = threading.Event()
        threading.Thread(
            target=self._write_input, daemon=True, name="UDB terminal input"
        ).start()
        self.thread = threading.Thread(
            target=self._pump, name=f"udb-pump-{self.id}", daemon=True
        )
        self.thread.start()

    def _pump(self):
        try:
            assert self.process.stdout
            while True:
                piece = os.read(self.process.stdout.fileno(), 4096)
                if not piece:
                    break
                with self.lock:
                    self.tail = (self.tail + piece)[-2048:]
                    self.last_output = time.monotonic()
                    self.chunks.append((self.position, piece))
                    self.position += len(piece)
                    while (
                        self.chunks
                        and self.position - self.chunks[0][0] > MAX_BUFFER_BYTES
                    ):
                        self.chunks.popleft()
                self.arrival.set()
        finally:
            self.done = True
            self.arrival.set()

    def read(self, offset: int | None, wait_ms: int = 100) -> dict[str, Any]:
        wait_ms = max(0, min(1000, int(wait_ms)))
        if wait_ms:
            self.arrival.wait(wait_ms / 1000)
            self.arrival.clear()
        with self.lock:
            start = self.chunks[0][0] if self.chunks else self.position
            cursor = self.read_cursor if offset is None else int(offset)
            stale = cursor < start
            cursor = max(cursor, start)
            output = bytearray()
            for chunk_start, chunk in self.chunks:
                chunk_end = chunk_start + len(chunk)
                if chunk_end <= cursor:
                    continue
                part = chunk[max(0, cursor - chunk_start) :]
                available = MAX_TERMINAL_OUTPUT_BYTES - len(output)
                if available <= 0:
                    break
                output += part[:available]
                cursor += min(len(part), available)
            raw = bytes(output)
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            decoded = decoder.decode(raw, final=self.done and cursor >= self.position)
            pending = decoder.getstate()[0]
            if pending:
                # Incomplete UTF-8 bytes remain available at the next offset.
                cursor -= len(pending)
                raw = raw[: -len(pending)]
            code = self.process.poll()
            self.read_cursor = cursor
        return {
            "session_id": self.id,
            "state": "RUNNING"
            if code is None or not self.done
            else ("PASS" if code == 0 else "ERROR"),
            "exit_code": code,
            "output": decoded,
            "next_offset": cursor,
            "earliest_offset": start,
            "output_truncated": stale,
            "has_more": cursor < self.position,
            "completion_reason": self.reason,
            "output_base64": __import__("base64").b64encode(raw).decode(),
            "terminal_mode": self.mode,
            "output_format": "vt_utf8" if self.mode == "conpty" else "utf8",
        }

    def finish_io(self):
        if self.process.poll() is not None and self.capacity is not None:
            self.capacity.close()
        self.input_done.set()
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        if self.done:
            try:
                self.process.stdout.close()
            except (OSError, ValueError):
                pass

    def prompt(self, expected=""):
        if not isinstance(expected, str) or len(expected) > 256:
            raise ValueError("Literal prompt limit: 256 characters")
        with self.lock:
            text = self.tail.decode("utf-8", errors="replace").replace("\r", "")
            line = text.rsplit("\n", 1)[-1].strip()
            quiet = time.monotonic() - self.last_output >= 0.08
        match = (
            text.endswith(expected)
            if expected
            else bool(
                re.search(
                    r"^(>>>|\.\.\.) ?$|^PS .{0,200}> ?$|^[A-Za-z]:\\.{0,200}> ?$|.{0,200}[$#] ?$",
                    line,
                )
            )
        )
        return bool(quiet and match), line[-256:] if match else ""

    def _write_input(self):
        while not self.input_done.is_set():
            try:
                data = self.input_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                write_all(self.process.stdin, data)
            except (OSError, ValueError):
                self.input_done.set()
                return

    def send(self, text: str) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise RuntimeError("Terminal already finished")
        if self.input_done.is_set():
            raise RuntimeError(
                "Terminal input channel is closed; inspect output before retrying"
            )
        if not isinstance(text, str) or len(text.encode("utf-8")) > 10000:
            raise ValueError("Input limit: 10,000 UTF-8 bytes")
        try:
            self.input_queue.put_nowait(text.encode("utf-8"))
        except queue.Full:
            raise RuntimeError(
                "Terminal input queue full; inspect output before retrying"
            ) from None
        return dict(session_id=self.id, accepted_chars=len(text), input_queued=True)

    def stop(self, reason="stopped") -> dict[str, Any]:
        if self.process.poll() is None:
            self.reason = self.reason or reason
        self.input_done.set()
        self.process.close()
        if self.capacity is not None:
            self.capacity.close()
        try:
            self.process.stdin.close()
        except (OSError, ValueError):
            pass
        self.thread.join(timeout=1)
        if not self.thread.is_alive():
            try:
                self.process.stdout.close()
            except (OSError, ValueError):
                pass
        return dict(
            session_id=self.id,
            exit_code=self.process.poll(),
            state="STOPPED",
            completion_reason=self.reason,
        )
