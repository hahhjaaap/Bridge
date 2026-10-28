"""Real Qt layout and owner-control behavior using only disposable local state."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from udb.desktop import DesktopController
from udb.desktop_control import paused


@pytest.fixture
def manager(tmp_path, monkeypatch):
    from udb.manager import MainWindow, make_app

    app = make_app()
    app.setProperty("motion", False)
    host = dict(
        worker_ready=True,
        enabled=True,
        resources=dict(
            admission_ready=True, cpu_percent=12, ram_percent=40, available_mib=8000
        ),
        sessions=[],
        emergency_hotkey_available=True,
    )
    monkeypatch.setattr("udb.host_client.status", lambda agent: host)
    controller = DesktopController(tmp_path, monitor=False)
    window = MainWindow(tmp_path, controller)
    window.timer.stop()
    window.resize(960, 680)
    window.show()
    app.processEvents()
    yield window, host
    window.close()
    app.processEvents()


def test_emergency_controls_remain_visible_when_body_is_scrolled(manager):
    from PySide6.QtWidgets import QApplication, QScrollArea

    window, _ = manager
    assert window.stack.currentIndex() == 5
    scroll = window.stack.currentWidget().findChild(QScrollArea)
    scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
    QApplication.processEvents()
    for button in (
        window.desktop_pause_button,
        window.desktop_resume_button,
        window.host_stop_button,
    ):
        assert button.isVisible()
        assert window.rect().contains(button.mapTo(window, button.rect().center()))


def test_pause_and_resume_update_local_state_without_granting_access(manager):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window, _ = manager
    assert not window.desktop_resume_button.isEnabled()
    QTest.mouseClick(window.desktop_pause_button, Qt.MouseButton.LeftButton)
    assert paused(window.agent) and window.desktop_resume_button.isEnabled()
    assert "tạm dừng" in window.desktop_state_title.text()
    QTest.mouseClick(window.desktop_resume_button, Qt.MouseButton.LeftButton)
    assert not paused(window.agent)
    assert not (window.agent.state / "computer-access.dpapi").exists()


def test_browser_handoff_controls_reflect_real_session_state(manager):
    window, host = manager
    assert not window.browser_background_button.isEnabled()
    host["sessions"] = [
        dict(
            session_id="a" * 32,
            kind="browser",
            tabs=2,
            task="Synthetic task",
            presentation="visible",
        )
    ]
    window._refresh_host()
    assert window.browser_background_button.isEnabled()
    assert not window.browser_login_button.isEnabled()
    assert "Bạn điều khiển" in window.desktop_state_pill.text()
    row = window.host_sessions.topLevelItem(0)
    assert row.text(0) == "Browser riêng" and row.text(1) == "Bạn điều khiển"
    assert "a" * 32 not in " ".join(row.text(i) for i in range(3))


def test_unknown_worker_does_not_claim_ai_is_ready(manager):
    window, host = manager
    host["worker_ready"] = False
    window._refresh_host()
    assert "Chưa sẵn sàng" in window.desktop_state_pill.text()
    assert not window.browser_start_button.isEnabled()
    assert window.desktop_pause_button.isEnabled()


def test_unavailable_hotkey_has_a_visible_button_fallback(manager):
    window, host = manager
    assert "Backspace" in window.desktop_hotkey_hint.text()
    host["emergency_hotkey_available"] = False
    window._refresh_host()
    assert "chưa sẵn sàng" in window.desktop_hotkey_hint.text()
    assert "Backspace" not in window.desktop_hotkey_hint.text()
    assert window.desktop_pause_button.isEnabled()


def test_cards_stack_on_compact_window_without_horizontal_clipping(manager):
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QScrollArea

    window, host = manager
    host["sessions"] = [
        dict(
            session_id="b" * 32,
            kind="browser",
            tabs=1,
            task="Preview",
            presentation="background",
        )
    ]
    window._refresh_host()
    window.resize(1220, 820)
    QTest.qWait(50)
    browser_card = window.browser_start_button.parentWidget()
    sessions_card = window.host_sessions.parentWidget()
    assert sessions_card.x() > browser_card.x()
    window.resize(960, 680)
    QTest.qWait(50)
    assert window.width() == 960
    assert sessions_card.y() > browser_card.y()
    scroll = window.stack.currentWidget().findChild(QScrollArea)
    scroll.ensureWidgetVisible(window.host_sessions)
    QTest.qWait(50)
    viewport = scroll.viewport()
    assert viewport.rect().contains(
        window.host_sessions.mapTo(viewport, window.host_sessions.rect().center())
    )
    assert window.host_sessions.width() <= viewport.width()


def test_project_grant_labels_update_without_changing_project_records(manager):
    window, _ = manager
    root = window.agent.home / "projects" / "demo"
    root.mkdir(parents=True)
    window.agent.register_local("demo", str(root), True)
    window._refresh_projects()
    assert window.projects.currentItem().text(1) == "Ghi có giới hạn"
    window.agent.set_source_autonomy_local("demo", True)
    window._refresh_projects()
    assert window.projects.currentItem().text(1) == "AI tự sửa"
    assert window.source_auto.isChecked()
    window.agent.set_source_autonomy_local("demo", False)
    window._refresh_projects()
    assert window.projects.currentItem().text(1) == "Ghi có giới hạn"
    assert not window.source_auto.isChecked()


def test_replacing_a_protected_root_updates_the_list_with_the_same_row_count(manager):
    window, _ = manager
    first, second = window.agent.home / "first", window.agent.home / "second"
    first.mkdir()
    second.mkdir()
    window.agent.protect_local(str(first))
    window._refresh_jobs()
    assert window.protected.topLevelItem(0).text(0) == str(first)
    with window.agent.connect() as db:
        db.execute("DELETE FROM protected")
    window.agent.protect_local(str(second))
    window._refresh_jobs()
    assert window.protected.topLevelItem(0).text(0) == str(second)


def test_project_refresh_failure_does_not_block_desktop_pause_state(
    manager, monkeypatch
):
    from udb.desktop_control import pause_local

    window, _ = manager

    def unavailable():
        raise OSError("Synthetic unavailable project")

    monkeypatch.setattr(window, "_refresh_projects", unavailable)
    pause_local(window.agent, True)
    window.refresh()
    assert "Đã tạm dừng" in window.desktop_state_pill.text()
    assert window.desktop_resume_button.isEnabled()
    assert "Synthetic unavailable project" in window.footer_status.text()


def test_pending_payload_updates_preserve_the_selected_request(manager):
    import json
    import time

    from PySide6.QtCore import Qt

    window, _ = manager
    first, second = "c" * 32, "d" * 32
    payload = {"project": "demo", "path": "first.py", "diff": "reviewed preview"}
    with window.agent.connect() as db:
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
            (first, "write", json.dumps(payload), "", time.time(), "pending", "{}"),
        )
    window._refresh_requests()
    assert window.pending_title.text() == "first.py"
    payload["path"] = "changed.py"
    with window.agent.connect() as db:
        db.execute(
            "UPDATE requests SET payload=? WHERE id=?", (json.dumps(payload), first)
        )
        db.execute(
            "INSERT INTO requests VALUES (?,?,?,?,?,?,?)",
            (
                second,
                "write",
                json.dumps(payload),
                "",
                time.time() - 1,
                "pending",
                "{}",
            ),
        )
    window._refresh_requests()
    assert window.requests.currentItem().data(0, Qt.ItemDataRole.UserRole) == first
    assert window.pending_title.text() == "changed.py"
