"""Security and protocol regressions for UDB Unified. Runs only inside temporary files."""

from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import udb_companion
import udb_unified


def test_status():
    samples = [
        (
            {"content": [{"type": "text", "text": '{"error": "Permission denied"}'}]},
            "ERROR",
        ),
        (
            {
                "content": [
                    {"type": "text", "text": '{"success": false, "message": "failed"}'}
                ]
            },
            "ERROR",
        ),
        ({"content": [{"type": "text", "text": '{"result": "FAIL"}'}]}, "ERROR"),
        (
            {
                "content": [
                    {
                        "type": "text",
                        "text": "Error executing tool computer_files: Access denied",
                    }
                ]
            },
            "ERROR",
        ),
        (
            {
                "content": [
                    {"type": "text", "text": '{"status": "running", "exit_code": null}'}
                ]
            },
            "RUNNING",
        ),
        (
            {
                "content": [
                    {"type": "text", "text": '{"status": "exited", "exit_code": 0}'}
                ]
            },
            "PASS",
        ),
        (
            {
                "content": [
                    {"type": "text", "text": '{"status": "exited", "exit_code": 2}'}
                ]
            },
            "ERROR",
        ),
    ]
    for data, expected in samples:
        result = udb_unified.normalized_output("audit_tool", data)
        line = result["content"][0]["text"].splitlines()[0]
        assert line.startswith(expected + " |"), (expected, line)
        if expected == "ERROR":
            assert result["isError"] is True
    print("PASS | backend error/status classification")


def test_oversized_reply():
    stream = io.BytesIO()
    server = udb_unified.UnifiedProxy.__new__(udb_unified.UnifiedProxy)
    import threading

    server.send_lock = threading.Lock()
    with (
        patch.object(udb_unified, "MAX_LINE", 320),
        patch.object(udb_unified.sys, "stdout", SimpleNamespace(buffer=stream)),
    ):
        server.send(
            {
                "jsonrpc": "2.0",
                "id": 77,
                "result": {"content": [{"type": "text", "text": "x" * 500}]},
            }
        )
    result = stream.getvalue()
    assert result, "Oversized reply was silently dropped"
    obj = json.loads(result)
    assert obj.get("id") == 77 and obj.get("error", {}).get("code") == -32005, obj
    assert b"x" * 20 not in result
    print("PASS | oversized response produces a bounded MCP error")


def test_casefold_path_guards():
    with tempfile.TemporaryDirectory(prefix="udb-security-") as work:
        p = Path(work)
        (p / "NODE_MODULES").mkdir()
        (p / "NODE_MODULES" / "secret.txt").write_text("hidden", encoding="utf-8")
        sf = udb_companion.ScopedFiles(work)
        (p / "checkpoints").mkdir()
        (p / "checkpoints" / "data.txt").write_text("blocked")
        import os

        (p / "outside.txt").write_text("linked")
        os.link(p / "outside.txt", p / "alias.txt")
        for name in ["checkpoints/data.txt", "alias.txt"]:
            try:
                sf.path(name)
            except PermissionError:
                pass
            else:
                raise AssertionError("Protected data was readable: " + name)
        try:
            sf.path("NODE_MODULES/secret.txt")
        except PermissionError:
            pass
        else:
            raise AssertionError("Case-variant of protected folder was readable")
        try:
            sf.path(".Env", allow_missing=True)
        except PermissionError:
            pass
        else:
            raise AssertionError("Case-variant of protected file was readable")
    print("PASS | case-insensitive protected path guards")


if __name__ == "__main__":
    test_status()
    test_oversized_reply()
    test_casefold_path_guards()
    print("PASS | all regression scenarios")
