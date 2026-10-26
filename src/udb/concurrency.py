"""Shared limits and crash-safe slots for owned background work."""

from __future__ import annotations

import errno
import os
import threading
from pathlib import Path

from .core import PolicyError, reject_links

MAX_CONCURRENT = 10
_claim_lock = threading.Lock()


class Slot:
    def __init__(self, stream):
        self.stream = stream
        self.lock = threading.Lock()

    def close(self):
        with self.lock:
            if self.stream is not None:
                self.stream.close()
                self.stream = None


class TaskPool:
    def __init__(self, folder):
        self.folder = Path(folder)

    def claim(self):
        with _claim_lock:
            reject_links(self.folder)
            self.folder.mkdir(parents=True, exist_ok=True)
            for number in range(MAX_CONCURRENT):
                path = self.folder / f"slot-{number}.lock"
                reject_links(path)
                fd = os.open(
                    path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                stream = os.fdopen(fd, "r+b", buffering=0)
                try:
                    if os.fstat(fd).st_nlink != 1:
                        raise PolicyError("Linked capacity slot refused")
                    if os.fstat(fd).st_size == 0:
                        stream.write(b"0")
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return Slot(stream)
                except OSError as exc:
                    stream.close()
                    if exc.errno not in (errno.EACCES, errno.EAGAIN) and getattr(
                        exc, "winerror", None
                    ) not in (33, 36, 158):
                        raise
                except BaseException:
                    stream.close()
                    raise
        raise PolicyError(
            "Maximum 10 active background jobs; wait for completion or stop an owned job"
        )
