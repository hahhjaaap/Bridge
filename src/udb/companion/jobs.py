"""Background job state, cancellation and durable checkpoint lifecycle.

This module has no dependency on the search, transfer or UI layers. Existing
callers may still import Task and Cancelled from companion.background.
"""

from __future__ import annotations

import threading
import time
import uuid


class Cancelled(Exception):
    pass


class Task:
    def __init__(self, kind):
        self.id = uuid.uuid4().hex
        self.kind = kind
        self.state = "running"
        self.error = ""
        self.result = {}
        self.cancel = threading.Event()
        self.lock = threading.RLock()
        self.rows = []
        self.progress = {}
        self.worker = None
        self.thread = None
        self.committed = False
        self.deadline = time.monotonic() + 3600
        self._checkpoint_version = 0
        self._persisted_checkpoint_version = -1
        self.persist = lambda: None
        self.release = lambda: None
        self.recovery = None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()
        if time.monotonic() > self.deadline:
            raise TimeoutError("Background job exceeded 3600 seconds")

    def commit(self, fn):
        with self.lock:
            self.check()
            self.persist()
            fn()
            self.committed = True
            self._checkpoint_version += 1
            self.persist()

    def start(self, fn):
        def run():
            try:
                result = fn(self)
                with self.lock:
                    self.result = result
                    self.state = "completed"
                    self._checkpoint_version += 1
            except Cancelled:
                with self.lock:
                    self.state = "cancelled"
                    self._checkpoint_version += 1
            except Exception as exc:
                with self.lock:
                    self.state = "error"
                    self.error = type(exc).__name__ + ": " + str(exc)
                    self._checkpoint_version += 1
            finally:
                try:
                    self.persist()
                finally:
                    self.release()

        self.thread = threading.Thread(target=run, daemon=True, name="UDB " + self.kind)
        self.thread.start()
        return self.id

    def read(self, offset=0, limit=50):
        if not 0 <= int(offset) <= 2000 or not 1 <= int(limit) <= 100:
            raise ValueError("Job offset/limit out of range")
        with self.lock:
            end = min(len(self.rows), int(offset) + int(limit))
            return dict(
                job_id=self.id,
                kind=self.kind,
                state=self.state,
                error=self.error,
                result=self.result,
                progress=dict(self.progress),
                results=self.rows[int(offset) : end],
                next_offset=end,
                has_more=end < len(self.rows),
                committed=self.committed,
                cancellation_requested=self.cancel.is_set(),
            )

    def stop(self):
        with self.lock:
            changed = False
            if not self.committed and not self.cancel.is_set():
                self.cancel.set()
                changed = True
            if self.worker:
                changed = True
            if changed:
                self._checkpoint_version += 1
            if self.worker:
                self.worker.terminate()
        return self.read()
