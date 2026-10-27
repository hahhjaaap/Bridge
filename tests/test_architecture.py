"""Architectural boundaries and public companion behavior regressions."""

from __future__ import annotations

import hashlib

import pytest

from udb.companion import dispatch
from udb.companion.runtime import Runtime
from udb.companion.schemas import TOOLS


def test_companion_schema_has_exactly_one_handler_per_tool():
    dispatch.validate_catalog()
    advertised = {item["name"] for item in TOOLS}
    implemented = (
        set(dispatch.NATIVE_HANDLERS)
        | set(dispatch.EXTENSION_HANDLERS)
        | set(dispatch.NAMES)
    )
    assert advertised == implemented
    assert len(advertised) == len(TOOLS)


def test_companion_dispatch_preserves_file_results(tmp_path):
    runtime = Runtime(str(tmp_path))
    try:
        written = runtime.tool(
            "file_write",
            {"path": "sample.txt", "content": "Xin chào", "expected_sha256": "NEW"},
        )
        assert not written.get("isError"), written
        read = runtime.tool("file_read", {"path": "sample.txt"})
        assert read["structuredContent"]["content"] == "Xin chào"
        expected = hashlib.sha256("Xin chào".encode("utf-8")).hexdigest()
        assert read["structuredContent"]["sha256"] == expected
        listed = runtime.tool("file_list", {"path": "."})
        assert "sample.txt" in listed["content"][0]["text"]
        stat = runtime.tool("file_stat", {"path": "sample.txt"})
        assert not stat.get("isError"), stat
        with pytest.raises(ValueError, match="Unknown tool"):
            runtime.tool("nonexistent_undocumented_tool", {})
    finally:
        runtime.close()


def test_feature_route_keeps_runtime_feature_instance(tmp_path, monkeypatch):
    runtime = Runtime(str(tmp_path))
    try:
        seen = []

        def fake_tool(name, args):
            seen.append((name, args))
            return {"route": "feature"}

        monkeypatch.setattr(runtime.features, "tool", fake_tool)
        result = runtime.tool("search_read", {"job_id": "synthetic"})
        assert result == {"route": "feature"}
        assert seen == [("search_read", {"job_id": "synthetic"})]
    finally:
        runtime.close()


def test_extension_route_keeps_canonical_extension_handler(tmp_path, monkeypatch):
    runtime = Runtime(str(tmp_path))
    try:
        seen = []
        monkeypatch.setattr(
            dispatch,
            "call_extension",
            lambda rt, name, args: seen.append((rt, name, args)) or {"ok": True},
        )
        assert runtime.tool("file_stat", {"path": "."}) == {"ok": True}
        assert seen == [(runtime, "file_stat", {"path": "."})]
    finally:
        runtime.close()


def test_manager_dialogs_have_a_single_canonical_module():
    pytest.importorskip("PySide6.QtWidgets")
    from udb import manager, manager_dialogs

    assert manager.SourceDialog is manager_dialogs.SourceDialog
    assert manager.ConnectionDialog is manager_dialogs.ConnectionDialog
    assert manager.ApprovalDialog is manager_dialogs.ApprovalDialog
