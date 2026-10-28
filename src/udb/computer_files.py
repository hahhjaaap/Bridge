"""Bounded file operations for an already owner-enabled computer session."""

from __future__ import annotations

import base64
import hashlib
import os
import shutil
import uuid
from pathlib import Path

from .core import RESERVED, PolicyError, reject_links

MAX_FILE = 8 * 1024 * 1024
_BLOCK_BYTES = 256 * 1024


class Files:
    def __init__(self, agent):
        self.agent = agent

    def path(self, value):
        if not isinstance(value, str) or not value or "\0" in value:
            raise PolicyError("An absolute local file path is required")
        p = Path(value)
        if not p.is_absolute() or ".." in p.parts:
            raise PolicyError("An absolute path without parent traversal is required")
        if os.name == "nt" and (str(p).startswith("\\\\") or ":" in str(p)[2:]):
            raise PolicyError(
                "UNC/device paths and alternate data streams are not supported"
            )
        for part in p.parts[1:]:
            if part.rstrip(". ") != part or part.split(".")[0].upper() in RESERVED:
                raise PolicyError("Reserved path component")
        reject_links(p)
        self.agent.check_protected(p)
        return p

    def data(self, p):
        with p.open("rb") as stream:
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise PolicyError(
                "File API limit is 8 MiB; use an explicitly authorized program for larger files"
            )
        return data

    def digest(self, p):
        return self._scan(p)[0]

    def _scan(self, p, offset=0, limit=0, sink=None):
        """Hash one bounded byte stream, retaining only the requested range."""
        hasher = hashlib.sha256()
        size = 0
        parts = []
        with p.open("rb") as stream:
            while block := stream.read(min(_BLOCK_BYTES, MAX_FILE + 1 - size)):
                start = size
                size += len(block)
                if size > MAX_FILE:
                    raise PolicyError(
                        "File API limit is 8 MiB; use an explicitly authorized program for larger files"
                    )
                hasher.update(block)
                if sink is not None:
                    sink.write(block)
                lo, hi = max(offset, start), min(offset + limit, size)
                if lo < hi:
                    parts.append(block[lo - start : hi - start])
        return hasher.hexdigest(), size, b"".join(parts)

    def check_expected(self, p, expected):
        if expected == "NEW":
            if p.exists():
                raise PolicyError("Create-only destination already exists")
        elif not p.is_file() or expected != self.digest(p):
            raise PolicyError(
                "File changed or expected SHA256 is missing; read it again before writing"
            )

    def dispatch(
        self,
        action,
        path,
        destination="",
        content="",
        encoding="utf-8",
        expected_sha256="",
        offset=0,
        limit=200,
    ):
        p = self.path(path)
        if action == "stat":
            return self._stat(p)
        if action == "list":
            return self._list(p, offset, limit)
        if action == "read":
            return self._read(p, encoding, offset, limit)
        if action == "mkdir":
            p.mkdir()
            return dict(created=True, path=str(p))
        if action in ("copy", "move"):
            return self._copy_move(action, p, destination, expected_sha256)
        if action == "write":
            return self._write(p, content, encoding, expected_sha256)
        raise PolicyError("File actions: stat, list, read, write, mkdir, copy, move")

    def _stat(self, p):
        st = p.stat()
        return dict(
            path=str(p),
            is_directory=p.is_dir(),
            bytes=st.st_size,
            modified=st.st_mtime,
            sha256=self.digest(p) if p.is_file() and st.st_size <= MAX_FILE else None,
        )

    def _list(self, p, offset, limit):
        if (
            not isinstance(offset, int)
            or not 0 <= offset <= 10000
            or not isinstance(limit, int)
            or not 1 <= limit <= 500
        ):
            raise PolicyError("Listing offset 0..10000; limit 1..500")
        rows = []
        skipped = 0
        more = False
        with os.scandir(p) as entries:
            for entry in entries:
                try:
                    q = self.path(entry.path)
                    if skipped < offset:
                        skipped += 1
                        continue
                    if len(rows) == limit:
                        more = True
                        break
                    st = q.stat()
                    rows.append(
                        dict(name=q.name, is_directory=q.is_dir(), bytes=st.st_size)
                    )
                except (OSError, PolicyError):
                    continue
        return dict(
            path=str(p),
            entries=rows,
            more=more,
            next_offset=offset + len(rows) if more else None,
        )

    def _read(self, p, encoding, offset, limit):
        if (
            not isinstance(offset, int)
            or not 0 <= offset <= MAX_FILE
            or not isinstance(limit, int)
            or not 1 <= limit <= 512 * 1024
        ):
            raise PolicyError("Read offset 0..8 MiB; limit 1..512 KiB")
        if encoding not in ("base64", "utf-8"):
            raise PolicyError("Encoding must be utf-8 or base64")
        checksum, size, part = self._scan(p, offset, limit)
        if encoding == "base64":
            value = base64.b64encode(part).decode("ascii")
        elif encoding == "utf-8":
            try:
                value = part.decode("utf-8")
            except UnicodeDecodeError:
                raise PolicyError(
                    "Read this byte range as base64; it is binary or splits a UTF-8 character"
                ) from None
        return dict(
            path=str(p),
            content=value,
            encoding=encoding,
            sha256=checksum,
            bytes=size,
            offset=offset,
            next_offset=offset + len(part) if offset + len(part) < size else None,
        )

    def _copy_move(self, action, p, destination, expected_sha256):
        dst = self.path(destination)
        if not p.is_file() or dst.exists() or p == dst:
            raise PolicyError(
                "Copy/move requires an existing regular file and a new destination"
            )
        self.check_expected(p, expected_sha256)

        def stage(stream):
            checksum, size, _ = self._scan(self.path(str(p)), sink=stream)
            if checksum != expected_sha256:
                raise PolicyError(
                    "Source changed after SHA256 verification; nothing copied"
                )
            return checksum, size

        result = self._commit(dst, "NEW", stage)
        if action == "move":
            self.path(str(p))
            self.check_expected(p, expected_sha256)
            p.unlink()
        return dict(
            path=str(dst),
            sha256=result["sha256"],
            moved=action == "move",
        )

    def _write(self, p, content, encoding, expected_sha256):
        if not isinstance(content, str) or len(content) > MAX_FILE * 2:
            raise PolicyError("File content too large")
        if encoding == "base64":
            try:
                data = base64.b64decode(content, validate=True)
            except ValueError:
                raise PolicyError("Invalid base64 content") from None
        elif encoding == "utf-8":
            data = content.encode("utf-8")
        else:
            raise PolicyError("Encoding must be utf-8 or base64")
        return self.write_bytes(str(p), data, expected_sha256)

    def write_bytes(self, path, data, expected_sha256):
        """Internal binary write; uses the same path/hash/commit checks as MCP."""
        if not isinstance(data, bytes) or len(data) > MAX_FILE:
            raise PolicyError("File content exceeds 8 MiB")
        p = self.path(path)

        def stage(stream):
            stream.write(data)
            return hashlib.sha256(data).hexdigest(), len(data)

        return self._commit(p, expected_sha256, stage)

    def _commit(self, p, expected_sha256, stage):
        self.check_expected(p, expected_sha256)
        recovery = None
        if expected_sha256 != "NEW" and p.exists():
            folder = self.agent.state / "computer-recovery"
            reject_links(folder)
            folder.mkdir(exist_ok=True)
            recovery = uuid.uuid4().hex
            shutil.copyfile(p, folder / recovery)
        temp = p.with_name(".udb-" + uuid.uuid4().hex + ".tmp")
        try:
            with temp.open("xb") as stream:
                checksum, size = stage(stream)
            self.path(str(p))
            self.check_expected(p, expected_sha256)
            if expected_sha256 == "NEW":
                # Atomic no-replace on Windows; link+unlink on POSIX for tests.
                if os.name == "nt":
                    temp.rename(p)
                else:
                    os.link(temp, p)
                    temp.unlink()
            else:
                temp.replace(p)
        finally:
            temp.unlink(missing_ok=True)
        self.agent.audit("computer_file_write", dict(bytes=size, recovery_id=recovery))
        return dict(
            path=str(p),
            sha256=checksum,
            bytes=size,
            recovery_id=recovery,
        )
