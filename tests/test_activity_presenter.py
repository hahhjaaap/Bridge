"""Tool-call correlation and uncertainty use disposable activity records."""

import sqlite3
import time

from udb.activity import Activity
from udb.activity_presenter import ActivityFeed, describe, summary


def test_parallel_completion_does_not_hide_another_unanswered_call(tmp_path):
    writer = Activity(tmp_path)
    feed = ActivityFeed(tmp_path)
    writer.emit("call", "host_browser", "first", details={"action": "click"})
    writer.emit("call", "spreadsheet", "second")
    writer.emit("result", "spreadsheet", "second", status="PASS")
    rows = feed.poll()
    assert len(rows) == 2 and rows[0]["status"] == "PASS"
    assert summary(rows)[0] == "Đang gọi công cụ"
    assert "Nhấp trên trang" in summary(rows)[1]
    writer.emit("result", "host_browser", "first", status="ERROR")
    assert summary(feed.poll())[0] == "Cần kiểm tra"


def test_old_call_and_async_result_do_not_claim_a_task_is_currently_running(tmp_path):
    now = time.time()
    call = dict(kind="call", tool="host_browser", at=now - 61)
    assert summary([call], now=now)[0] == "Chưa nhận được kết quả"
    assert (
        summary([dict(kind="result", tool="terminal", status="RUNNING")])[0]
        == "Đã giao tác vụ nền"
    )


def test_feed_is_bounded_and_recovers_from_log_replacement(tmp_path):
    writer, feed = Activity(tmp_path), ActivityFeed(tmp_path)
    for i in range(45):
        writer.emit("result", "host_browser", str(i), status="PASS")
    assert len(feed.poll()) == 40
    assert len(feed.poll()) == 40
    (tmp_path / "state/activity.sqlite3").unlink()
    writer.emit("result", "terminal", "new", status="ERROR")
    rows = feed.poll()
    assert len(rows) == 1 and rows[0]["call_id"] == "new"


def test_malformed_metadata_is_literal_bounded_and_does_not_break_presentation(
    tmp_path,
):
    writer, feed = Activity(tmp_path), ActivityFeed(tmp_path)
    writer.emit("call", "<b>untrusted</b>", "call", details={"value": "secret text"})
    path = tmp_path / "state/activity.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("UPDATE activity SET at=?,details=?", (1e300, '{"action":[]}'))
    rows = feed.poll()
    assert rows[0]["at"] == 0 and "untrusted" in describe(rows[0])
    assert summary(rows)[0] == "Chưa nhận được kết quả"
    assert "secret text" not in str(rows)
