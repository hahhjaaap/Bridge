"""UDB Unified optimization regressions. Operates only in disposable temp projects."""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from udb_companion import MAX_TERMINAL_OUTPUT_BYTES, Runtime, ScopedFiles
from udb_extensions import _file_copy
from udb_unified import UnifiedProxy


def check_atomic_create_and_copy() -> None:
    with tempfile.TemporaryDirectory(prefix="udb-opt-files-") as root:
        folder = Path(root).resolve()
        files = ScopedFiles(root)
        src = folder / "source.txt"
        src.write_text("keep-source", encoding="utf-8")
        dest = folder / "other.txt"
        baseline_open = Path.open

        def racing_open(self, mode="r", *args, **kwargs):
            if self == dest and mode == "xb":
                dest.write_text("concurrent-owner", encoding="utf-8")
                raise FileExistsError("simulated concurrent create")
            return baseline_open(self, mode, *args, **kwargs)

        stub = SimpleNamespace(files=files)
        with patch.object(Path, "open", racing_open):
            try:
                _file_copy(stub, {"source": "source.txt", "destination": "other.txt"})
                raise AssertionError("Copy should have failed with destination exists")
            except FileExistsError:
                pass
        assert dest.read_text(encoding="utf-8") == "concurrent-owner"

        created = folder / "new.txt"
        link = os.link

        def racing_link(source, target, *args, **kwargs):
            created.write_text("not-ours", encoding="utf-8")
            return link(source, target, *args, **kwargs)

        with patch("udb_companion.os.link", racing_link):
            try:
                files.write("new.txt", "our-content", "NEW")
                raise AssertionError("Create-only should reject a concurrent file")
            except FileExistsError:
                pass
        assert created.read_text(encoding="utf-8") == "not-ours"

        own = files.write("created.txt", "created", "NEW")
        assert own["created"] and (folder / "created.txt").read_text() == "created"
        previous = own["sha256"]
        updated = files.write("created.txt", "modified", previous)
        assert (
            not updated["created"]
            and (folder / "created.txt").read_text() == "modified"
        )
        print("PASS | create-only and copy never delete/overwrite concurrent files")


def check_file_memory() -> None:
    with tempfile.TemporaryDirectory(prefix="udb-opt-memory-") as root:
        p = Path(root) / "large.txt"
        block = b"abcdefgh" * (1024 * 128)
        with p.open("wb") as fp:
            for _ in range(6):
                fp.write(block)
        expected_sha = hashlib.sha256(p.read_bytes()).hexdigest()
        tracemalloc.start()
        result = ScopedFiles(root).read("large.txt", offset=4096, limit=4096)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert result["sha256"] == expected_sha
        assert result["content"] == "abcdefgh" * 512
        assert peak < 4 * 1024 * 1024, f"Read allocated excessive memory: {peak}"
        print(f"PASS | streamed file read memory peak {peak // 1024} KiB")


def check_terminal_pagination() -> None:
    with tempfile.TemporaryDirectory(prefix="udb-opt-terminal-") as root:
        rt = Runtime(root, enable_terminal=True)
        try:
            job = rt.start(
                sys.executable,
                ["-u", "-c", "import sys;sys.stdout.write('Q'*36000)"],
                ".",
                0,
            )
            session = rt.sessions[job["session_id"]]
            session.process.wait(timeout=10)
            for _ in range(50):
                if session.done:
                    break
                time.sleep(0.02)
            out = []
            offset = 0
            for _ in range(20):
                row = session.read(offset, 0)
                text = row["output"]
                assert len(text.encode("utf-8")) <= MAX_TERMINAL_OUTPUT_BYTES
                out.append(text)
                assert row["next_offset"] >= offset
                offset = row["next_offset"]
                if not row["has_more"]:
                    break
            assert "".join(out) == "Q" * 36000, len("".join(out))
            print("PASS | terminal output paging retains all 36,000 characters")
        finally:
            rt.shutdown()


def check_tool_schema_cache() -> None:
    class Backend:
        def __init__(self, name, tool):
            self.name = name
            self.tool = tool
            self.calls = 0

        def request(self, method):
            self.calls += 1
            assert method == "tools/list"
            return {
                "result": {
                    "tools": [{"name": self.tool, "inputSchema": {"type": "object"}}]
                }
            }

    a, b = Backend("original", "a"), Backend("companion", "b")
    proxy = UnifiedProxy(a, b)
    for n in range(60):
        result = proxy.tools_list()
        assert len(result["tools"]) == 2
        if n == 0:
            result["tools"][0]["name"] = "hijacked"  # should not pollute cache
    assert a.calls == b.calls == 1, (a.calls, b.calls)
    assert proxy.tools_list()["tools"][0]["name"] == "a"
    print("PASS | MCP tool schemas cached and protected against caller mutation")


def check_bounded_frame() -> None:
    import threading

    import udb_unified as mod

    oversized = b"X" * 600 + b"\n"
    malformed = b"{broken-json}\n"
    stream = io.BytesIO(oversized + malformed)
    errors = []
    server = UnifiedProxy.__new__(UnifiedProxy)
    server.primary = SimpleNamespace(notification=lambda x: None)
    server.companion = SimpleNamespace(notification=lambda x: None)
    server.active_limit = threading.BoundedSemaphore(2)
    server.send = errors.append
    with (
        patch.object(mod, "MAX_LINE", 256),
        patch.object(mod.sys, "stdin", SimpleNamespace(buffer=stream)),
    ):
        server.serve()
    assert len(errors) == 2, errors
    assert errors[0]["error"]["code"] == -32700
    assert errors[1]["error"]["code"] == -32700
    print("PASS | bounded input frames and invalid JSON return protocol errors")


def main():
    check_atomic_create_and_copy()
    check_file_memory()
    check_terminal_pagination()
    check_tool_schema_cache()
    check_bounded_frame()
    print("PASS | all performance and concurrency regressions")


if __name__ == "__main__":
    main()
