"""Scoped source-file operations and path validation, independent of MCP."""

from __future__ import annotations

import fnmatch
import hashlib
import heapq
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from udb.path_policy import is_data_container

from .constants import (
    HIDDEN_DIRS_CASEFOLD,
    MAX_FILE_BYTES,
    MAX_READ_BYTES,
    MAX_SCAN_FILES,
    PROTECTED_NAMES,
    PROTECTED_PREFIXES,
    PROTECTED_SUFFIXES,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & 0x400)


class ScopedFiles:
    def __init__(self, root: str):
        home = Path(root).expanduser().absolute()
        if not home.is_dir() or is_reparse(home):
            raise ValueError("Project root must exist and must not be a link")
        self.root = home.resolve(strict=True)

    def path(self, given: str, allow_missing: bool = False) -> Path:
        if not isinstance(given, str) or not given or "\x00" in given:
            raise ValueError("Missing or invalid file path")
        p = Path(given)
        raw = (p if p.is_absolute() else self.root / p).absolute()
        full = Path(os.path.normpath(str(raw)))
        if os.name == "nt" and (str(full).startswith("\\\\") or ":" in str(full)[2:]):
            raise PermissionError("UNC and alternate data streams are not allowed")
        try:
            rel = full.relative_to(self.root)
        except ValueError:
            raise PermissionError("Path is outside the authorized project") from None
        if any(
            part.casefold() in PROTECTED_NAMES
            or is_data_container(part)
            or part.casefold().startswith(PROTECTED_PREFIXES)
            or part.lower().endswith(tuple(PROTECTED_SUFFIXES))
            for part in rel.parts
        ):
            raise PermissionError("Protected file is not accessible")
        if any(
            part.casefold().startswith((".udb-stage-", ".udb-upload-", ".udb-write-"))
            or part.casefold() in HIDDEN_DIRS_CASEFOLD
            for part in rel.parts
        ):
            raise PermissionError("Protected directory is not accessible")
        walk = self.root
        for item in rel.parts:
            walk = walk / item
            if is_reparse(walk):
                raise PermissionError("Links / reparse points are not allowed")
        if not full.resolve(strict=False).is_relative_to(self.root):
            raise PermissionError("Resolved path is outside the authorized project")
        if full.is_file() and full.stat().st_nlink > 1:
            raise PermissionError("Hardlinked data files are not allowed")
        if not allow_missing and not full.exists():
            raise FileNotFoundError("File or directory not found")
        return full

    def list(self, path: str = ".", limit: int = 100) -> dict[str, Any]:
        base = self.path(path)
        if not base.is_dir():
            raise NotADirectoryError("Target is not a directory")
        count = max(1, min(int(limit), 500))
        # Stream directory entries and keep only the first 'count' safe names.
        # Sorting all entries can allocate unbounded memory in a large folder.
        def allowed_entries():
            with os.scandir(base) as entries:
                for entry in entries:
                    try:
                        yield self.path(entry.path)
                    except (PermissionError, ValueError, FileNotFoundError):
                        continue

        items = []
        for safe in heapq.nsmallest(
            count, allowed_entries(), key=lambda p: p.name.casefold()
        ):
            items.append(
                {
                    "name": safe.name,
                    "kind": "dir" if safe.is_dir() else "file",
                    "size": safe.stat().st_size if safe.is_file() else None,
                }
            )
        return {
            "path": str(base.relative_to(self.root)),
            "items": items,
            "limit": count,
        }

    def read(self, path: str, offset: int = 0, limit: int = 8192) -> dict[str, Any]:
        p = self.path(path)
        if not p.is_file():
            raise ValueError("Target is not a file")
        if p.stat().st_size > MAX_FILE_BYTES:
            raise ValueError("File is too large; use a dedicated large-file processor")
        start = max(0, int(offset))
        count = max(1, min(MAX_READ_BYTES, int(limit)))
        hasher = hashlib.sha256()
        with p.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > MAX_FILE_BYTES:
                raise ValueError(
                    "File is too large; use a dedicated large-file processor"
                )
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(block)
            stream.seek(start)
            view = stream.read(count)
        try:
            displayed = view.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "Read range must contain valid UTF-8; binary files are unsupported"
            ) from exc
        return {
            "path": str(p.relative_to(self.root)),
            "content": displayed,
            "sha256": hasher.hexdigest(),
            "size": size,
            "next_offset": start + len(view),
            "remaining": max(0, size - start - len(view)),
        }

    def write(
        self, path: str, content: str, expected_sha256: str = "NEW"
    ) -> dict[str, Any]:
        p = self.path(path, allow_missing=True)
        if not isinstance(content, str):
            raise ValueError("content must be text")
        data = content.encode("utf-8")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("8 MiB write limit exceeded")
        p.parent.mkdir(parents=True, exist_ok=True)
        is_existing = p.exists()
        if is_existing and not p.is_file():
            raise ValueError("Target is not a normal file")
        if expected_sha256 == "NEW":
            if is_existing:
                raise FileExistsError(
                    "File already exists. Supply its current SHA256 to replace."
                )
        elif not re.fullmatch(r"[0-9a-f]{64}", expected_sha256, re.I):
            raise ValueError("expected_sha256 must be NEW or a 64-char SHA256")
        if is_existing:
            if p.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("Existing file exceeds 8 MiB guarded-write limit")
            previous = p.read_bytes()
            if digest(previous).lower() != expected_sha256.lower():
                raise ValueError("File changed since reading; write cancelled")
        elif expected_sha256 != "NEW":
            raise FileNotFoundError("Cannot replace a file that no longer exists")
        handle, temp_name = tempfile.mkstemp(prefix=".udb-write-", dir=str(p.parent))
        try:
            with os.fdopen(handle, "wb") as target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            if not is_existing:
                # Atomic create-only: hard links refuse an existing destination.
                # Both names are on the same filesystem.
                os.link(temp_name, p)
            else:
                # Best effort CAS; noncooperating external writers may still
                # race a replacement. Never suppress failed comparisons.
                if digest(p.read_bytes()).lower() != expected_sha256.lower():
                    raise ValueError("File changed before commit; write cancelled")
                if not p.name.startswith(".udb-companion-test-"):
                    recovery = self.root / ".udb-recovery"
                    if is_reparse(recovery):
                        raise PermissionError("Recovery directory is redirected")
                    recovery.mkdir(exist_ok=True)
                    if is_reparse(recovery):
                        raise PermissionError("Recovery directory is redirected")
                    backup = recovery / (
                        digest(str(p).encode())[:12]
                        + "-"
                        + str(int(time.time() * 1000))
                        + ".bak"
                    )
                    with backup.open("xb") as b:
                        b.write(previous)
                        b.flush()
                        os.fsync(b.fileno())
                os.replace(temp_name, p)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        return {
            "path": str(p.relative_to(self.root)),
            "bytes": len(data),
            "sha256": digest(data),
            "created": not is_existing,
        }

    def replace(
        self, path: str, old: str, new: str, expected_sha256: str, occurrences: int = 1
    ) -> dict[str, Any]:
        p = self.path(path)
        data = p.read_bytes()
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("File too large")
        if digest(data) != expected_sha256:
            raise ValueError("File changed since reading; edit cancelled")
        source = data.decode("utf-8")
        actual = source.count(old) if old else 0
        if actual != int(occurrences) or actual == 0:
            raise ValueError(
                f"Expected {occurrences} matches, found {actual}; edit cancelled"
            )
        return self.write(path, source.replace(old, new), expected_sha256) | {
            "replacements": actual
        }

    def search(
        self, path: str = ".", pattern: str = "*", text: str = "", limit: int = 30
    ) -> dict[str, Any]:
        root = self.path(path)
        if not root.is_dir():
            raise NotADirectoryError("Search path must be a folder")
        matches = []
        scanned = 0
        cap = max(1, min(200, int(limit)))
        for base, dirs, files in os.walk(root, followlinks=False):
            # Do not descend into protected trees just to reject every file.
            # This also avoids scanning large datasets, model weights and secrets.
            allowed_dirs = []
            for name in dirs:
                if name.casefold() in HIDDEN_DIRS_CASEFOLD:
                    continue
                try:
                    self.path(str(Path(base) / name))
                except (PermissionError, ValueError, FileNotFoundError, OSError):
                    continue
                allowed_dirs.append(name)
            dirs[:] = allowed_dirs
            for name in files:
                scanned += 1
                if scanned > MAX_SCAN_FILES:
                    return {
                        "matches": matches,
                        "scanned": MAX_SCAN_FILES,
                        "truncated": True,
                    }
                if not fnmatch.fnmatch(name, pattern):
                    continue
                p = Path(base) / name
                try:
                    p = self.path(str(p))
                    if not p.is_file():
                        continue
                    if text:
                        if p.stat().st_size > 1024 * 1024:
                            continue
                        if (
                            text.casefold()
                            not in p.read_text(
                                encoding="utf-8", errors="replace"
                            ).casefold()
                        ):
                            continue
                except (OSError, ValueError, PermissionError):
                    continue
                matches.append(str(p.relative_to(self.root)))
                if len(matches) >= cap:
                    return {"matches": matches, "scanned": scanned, "truncated": True}
        return {"matches": matches, "scanned": scanned, "truncated": False}
