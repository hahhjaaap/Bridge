"""Compatibility and accounting-friendly search regressions after job extraction."""

from __future__ import annotations

import time
from pathlib import Path

from udb.companion.background import Background, Cancelled, Task, search_worker
from udb.companion.files import ScopedFiles
from udb.companion.jobs import Cancelled as JobCancelled
from udb.companion.jobs import Task as JobTask
from udb.companion.search import SearchMixin
from udb.companion.search import search_worker as isolated_search_worker


def test_background_keeps_existing_public_imports():
    assert Task is JobTask
    assert Cancelled is JobCancelled
    assert search_worker is isolated_search_worker
    assert issubclass(Background, SearchMixin)


def _wait(bg: Background, job_id: str):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        row = bg.task(job_id, "read", 0, 100)
        if row["state"] != "running":
            return row
        time.sleep(0.025)
    raise AssertionError("Search worker did not finish in time")


def test_search_unicode_accounting_content_and_filenames(tmp_path):
    (tmp_path / "HÓA ĐƠN QUÝ 3.txt").write_text(
        "Bảng kê HÓA ĐƠN tháng 9\nDữ liệu không khớp\n", encoding="utf-8"
    )
    (tmp_path / "khac.txt").write_text("Dòng không liên quan\n", encoding="utf-8")
    bg = Background(ScopedFiles(str(tmp_path)))
    try:
        content_job = bg.search(text="hóa đơn", search_type="content")
        content = _wait(bg, content_job["job_id"])
        assert content["state"] == "completed", content
        assert any(
            r["line"] == 1 and "HÓA ĐƠN" in r["text"] for r in content["results"]
        )
        file_job = bg.search(text="hóa đơn", search_type="files")
        filenames = _wait(bg, file_job["job_id"])
        assert filenames["state"] == "completed", filenames
        assert any("HÓA ĐƠN QUÝ 3.txt" in r["path"] for r in filenames["results"])
    finally:
        bg.close()


def test_iterative_file_copy_preserves_nested_tree(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    nested = source
    for _ in range(30):
        nested = nested / "d"
        nested.mkdir()
    (nested / "hoa_don.txt").write_text("Thuế GTGT", encoding="utf-8")
    bg = Background(ScopedFiles(str(tmp_path)))
    try:
        job = bg.file_operation("copy", source="source", destination="backup")
        result = _wait(bg, job["job_id"])
        assert result["state"] == "completed", result
        assert result["result"]["entries"] == 32
        assert (tmp_path / "backup" / Path(*(["d"] * 30)) / "hoa_don.txt").read_text(
            encoding="utf-8"
        ) == "Thuế GTGT"
    finally:
        bg.close()
