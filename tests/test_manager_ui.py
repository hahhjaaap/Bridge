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
