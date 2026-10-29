"""Real Qt quick controls; owner state, lifecycle and queued activity updates."""

import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QThreadPool

from udb.activity import Activity
from udb.desktop import DesktopController
from udb.desktop_control import paused


@pytest.fixture
def quick(tmp_path, monkeypatch):
    from udb.manager import MainWindow, make_app

    app = make_app()
    app.setProperty("motion", False)
    host = dict(
        worker_ready=True,
        enabled=True,
        sessions=[],
        emergency_hotkey_available=True,
        resources=dict(
            admission_ready=True, cpu_percent=10, ram_percent=30, available_mib=8000
        ),
    )
    monkeypatch.setattr("udb.host_client.status", lambda agent: host)
    win = MainWindow(tmp_path, DesktopController(Path(tmp_path), monitor=False))
    win.timer.stop()
    win.show()
    win.open_quick_controls()
    app.processEvents()
    yield win, win.quick_controls, host
    win._keep_in_tray = False
    win._quit_requested = True
    win.close()
    QThreadPool.globalInstance().waitForDone(2000)
    app.processEvents()
    app.setQuitOnLastWindowClosed(True)


def test_quick_pause_is_local_and_remains_available_during_other_work(quick):
    from PySide6.QtWidgets import QApplication

    win, panel, _ = quick
    panel.resize(480, 560)
    QApplication.processEvents()
    assert panel.height() <= 560
    for button in (
        panel.pause_button,
        panel.resume_button,
        panel.handoff_button,
        panel.stop_button,
        panel.expand_button,
    ):
        assert panel.rect().contains(button.mapTo(panel, button.rect().center()))
    win.busy = True
    panel.sync()
    assert not panel.handoff_button.isEnabled()
    panel.pause_button.click()
    assert paused(win.agent) and panel.resume_button.isEnabled()
    win.busy = False
    panel.resume_button.click()
    assert not paused(win.agent)
    assert not (win.agent.state / "computer-access.dpapi").exists()


def test_browser_return_keeps_its_label_while_resources_block_the_action(quick):
    win, panel, host = quick
    host["sessions"] = [
        dict(session_id="a" * 32, kind="browser", presentation="visible", tabs=1)
    ]
    host["resources"]["admission_ready"] = False
    win.refresh()
    assert "Trả về" in panel.handoff_button.text()
    assert not panel.handoff_button.isEnabled()
    assert "Bạn điều khiển" in panel.control_state.text()


def test_activity_reads_off_thread_and_never_marks_a_connection_as_work(quick):
    from PySide6.QtTest import QTest

    win, panel, _ = quick
    assert panel.activity_title.text() == "Chưa có hoạt động"
    Activity(win.agent.home).emit(
        "call", "host_browser", "synthetic", details={"action": "select_option"}
    )
    deadline = time.monotonic() + 3
    while panel.history.topLevelItemCount() == 0 and time.monotonic() < deadline:
        panel.poll_activity()
        QTest.qWait(20)
    assert panel.history.topLevelItemCount() == 1
    assert "Chọn dropdown" in panel.activity_detail.text()
    assert panel.activity_title.text() == "Đang gọi công cụ"
    Activity(win.agent.home).emit("call", "<b>untrusted</b>", "literal")
    deadline = time.monotonic() + 3
    while panel.history.topLevelItemCount() < 2 and time.monotonic() < deadline:
        panel.poll_activity()
        QTest.qWait(20)
    item = panel.history.topLevelItem(0)
    assert item.text(0) == "<b>untrusted</b>"
    assert "&lt;b&gt;" in item.toolTip(0) and "<b>" not in item.toolTip(0)


def test_activity_failure_preserves_emergency_controls(quick):
    from PySide6.QtTest import QTest

    win, panel, _ = quick
    QThreadPool.globalInstance().waitForDone(2000)
    QTest.qWait(10)

    def broken():
        raise OSError("do not display raw log failure")

    panel.feed.poll = broken
    panel.poll_activity()
    deadline = time.monotonic() + 3
    while panel.worker is not None and time.monotonic() < deadline:
        QTest.qWait(20)
    assert panel.activity_title.text() == "Nhật ký chưa cập nhật"
    assert "raw log" not in panel.activity_note.text()
    panel.pause_button.click()
    assert paused(win.agent)


def test_a_slow_log_reader_does_not_block_local_pause(quick):
    import threading

    from PySide6.QtTest import QTest

    win, panel, _ = quick
    QThreadPool.globalInstance().waitForDone(2000)
    QTest.qWait(10)
    entered, release = threading.Event(), threading.Event()

    def slow():
        entered.set()
        release.wait(timeout=3)
        return []

    panel.feed.poll = slow
    panel.poll_activity()
    try:
        deadline = time.monotonic() + 2
        while not entered.is_set() and time.monotonic() < deadline:
            QTest.qWait(10)
        assert entered.is_set()
        panel.pause_button.click()
        assert paused(win.agent) and panel.worker is not None
    finally:
        release.set()


def test_pause_write_failure_is_visible_and_does_not_claim_paused(quick, monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    win, panel, _ = quick
    warnings = []

    def fail(*args):
        raise OSError("Cannot persist local pause")

    monkeypatch.setattr("udb.desktop_control.pause_local", fail)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: warnings.append(args[2]))
    panel.pause_button.click()
    assert warnings == ["Cannot persist local pause"]
    assert not panel.resume_button.isEnabled() and not paused(win.agent)


def test_tray_unavailable_keeps_full_manager_reachable(quick, monkeypatch):
    from udb import manager_quick

    win, panel, _ = quick
    monkeypatch.setattr(
        manager_quick.QSystemTrayIcon, "isSystemTrayAvailable", lambda: False
    )
    assert panel.install_tray() is False
    assert panel.close() is True and win.isVisible()


def test_close_hides_to_tray_and_explicit_quit_closes_owned_resources(
    quick, monkeypatch
):
    from udb import manager_quick
    from udb.manager import make_app

    class Tray:
        @staticmethod
        def isSystemTrayAvailable():
            return True

        def __init__(self, *args):
            self.activated = SimpleNamespace(connect=lambda fn: None)
            self.visible = False

        def setContextMenu(self, menu):
            self.menu = menu

        def setToolTip(self, text):
            self.tooltip = text

        def show(self):
            self.visible = True

        def hide(self):
            self.visible = False

    win, panel, _ = quick
    monkeypatch.setattr(manager_quick, "QSystemTrayIcon", Tray)
    assert panel.install_tray()
    assert win.close() is False and not win.isVisible()
    assert not win.controller.closed.is_set()
    assert panel.close() is False and not panel.isVisible()
    panel.show_panel()
    win.busy = True
    assert panel.request_quit() is False and panel.isVisible()
    win.busy = False
    quit_calls = []
    monkeypatch.setattr(make_app(), "quit", lambda: quit_calls.append(True))
    close = win.controller.close
    failures = [True]

    def close_once():
        if failures:
            failures.pop()
            raise OSError("Synthetic cleanup failure")
        close()

    monkeypatch.setattr(win.controller, "close", close_once)
    monkeypatch.setattr("udb.manager.QMessageBox.warning", lambda *args: None)
    assert panel.request_quit() is False and not win._closing
    assert panel.isVisible() and panel.tray.visible and not quit_calls
    assert panel.request_quit() is True
    assert win.controller.closed.is_set() and not panel.tray.visible
    assert quit_calls == [True]
