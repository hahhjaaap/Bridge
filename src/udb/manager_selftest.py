"""Disposable GUI integration self-test kept outside the runtime UI module."""

from __future__ import annotations

import json
from pathlib import Path


def self_test(preview_dir=None):
    """Exercise the frozen Qt UI with disposable synthetic data, not the user's state."""
    import tempfile

    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QLabel, QWidget

    from .desktop import DesktopController
    from .manager import ApprovalDialog, MainWindow, SourceDialog, make_app

    app = make_app()
    app.setProperty("motion", False)
    captures = Path(preview_dir) if preview_dir else None
    if captures:
        captures.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="udb-gui-selftest-") as temp:
        home = Path(temp) / "bridge"
        source = Path(temp) / "source"
        source.mkdir()
        (source / "hello.py").write_text("value = 1\n", encoding="utf-8")
        ctl = DesktopController(home, monitor=False)
        ctl.agent.register_local("demo", str(source), True)
        request = ctl.agent.propose_write(
            "demo", "gui-proof.py", 'print("<b>literal source</b>")\n', "NEW"
        )
        win = MainWindow(home, ctl)
        win.show()
        app.processEvents()
        assert win.projects.topLevelItemCount() == 1
        assert win.selected_project["id"] == "demo"
        assert win.review_button.isEnabled()
        assert win.pending_rows[request["request_id"]]["status"] == "pending"
        QTest.mouseClick(win.nav_buttons[1], Qt.MouseButton.LeftButton)
        app.processEvents()
        assert win.stack.currentIndex() == 1 and win.requests.topLevelItemCount() == 1
        dialog = ApprovalDialog(ctl.agent, win.pending_rows[request["request_id"]], win)
        dialog.show()
        app.processEvents()
        assert not dialog.approve.isEnabled()
        assert dialog.full.toPlainText() == 'print("<b>literal source</b>")\n'
        if captures:
            assert dialog.grab().save(str(captures / "approval-dialog.png"))
        dialog.reviewed.setFocus()
        QTest.keyClick(dialog.reviewed, Qt.Key.Key_Space)
        app.processEvents()
        assert dialog.reviewed.isChecked(), "Review checkbox keyboard activation failed"
        assert dialog.approve.isEnabled()
        # Intentionally do not click Approve. A checked box must not write anything.
        assert not (source / "gui-proof.py").exists()
        assert ctl.agent.request_status(request["request_id"])["status"] == "pending"
        dialog.reject()
        app.processEvents()
        browse = SourceDialog(ctl.agent, "demo", win)
        found = False
        for i in range(browse.files.count()):
            item = browse.files.item(i)
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry.get("path") == "hello.py":
                browse.open_item(item)
                found = True
                break
        assert found and browse.code.toPlainText() == "value = 1\n"
        browse.accept()
        from . import desktop_vm

        original_available = desktop_vm.available
        try:
            desktop_vm.available = lambda: True
            proposal = ctl.agent.propose_desktop(
                "demo", "Synthetic GUI permission preview only", 600
            )
        finally:
            desktop_vm.available = original_available
        win.refresh()
        permission = ApprovalDialog(
            ctl.agent, win.pending_rows[proposal["request_id"]], win
        )
        permission.show()
        app.processEvents()
        assert permission.is_desktop and not permission.approve.isEnabled()
        assert "Sandbox guest only" in permission.preview.toPlainText()
        if captures:
            assert permission.grab().save(str(captures / "desktop-permission.png"))
        permission.reject()
        app.processEvents()
        assert ctl.agent.request_status(proposal["request_id"])["status"] == "pending"
        # Synthetic history exercises the observer tabs without launching a VM.
        jid = "a" * 32
        job = ctl.agent.state / "jobs" / jid
        (job / "input").mkdir(parents=True)
        (job / "output").mkdir()
        script = 'print("<b>visible observer proof</b>")\n'
        (job / "input" / "task.py").write_text(script, encoding="utf-8")
        (job / "output" / "output.log").write_text(
            "old line\n" * 5000 + "live <b>literal output</b>\n", encoding="utf-8"
        )
        with ctl.agent.connect() as c:
            c.execute(
                "INSERT INTO jobs VALUES (?,?,?)",
                (
                    jid,
                    "reviewing",
                    json.dumps(
                        {"project": "demo", "language": "python", "review_seconds": 60}
                    ),
                ),
            )
        win.refresh()
        win.jobs.setCurrentItem(win.jobs.topLevelItem(0))
        win.show_job()
        assert win.job_script.toPlainText() == script
        assert "live <b>literal output</b>" in win.job_output.toPlainText()
        # Exercise real layout and painting, including a first-time page switch,
        # compact desktop, and both motion settings. Never approve a request.
        for motion in (False, True):
            app.setProperty("motion", motion)
            for width, height in ((1220, 820), (960, 680)):
                win.resize(width, height)
                app.processEvents()
                for i in range(6):
                    QTest.mouseClick(win.nav_buttons[i], Qt.MouseButton.LeftButton)
                    QTest.qWait(220)
                    assert win.stack.currentIndex() == i
                    page = win.stack.currentWidget()
                    header = page.findChild(QWidget, "PageHeader")
                    title = header.findChild(QLabel, "H1")
                    subtitle = header.findChild(QLabel, "Subtitle")
                    assert title.isVisible() and subtitle.isVisible()
                    assert title.height() >= title.fontMetrics().height()
                    assert title.geometry().bottom() < subtitle.geometry().top(), (
                        "Header labels overlap"
                    )
                    assert header.width() <= page.width() and page.width() > 600
                    assert (
                        page.graphicsEffect() is None
                        and win.stack.graphicsEffect() is None
                    )
                    assert win.nav_buttons[i].isVisible()
                    if i == 0:
                        assert win.project_path.isReadOnly()
                        assert win.project_path.text() == str(source)
                        assert (
                            win.project_path.parentWidget()
                            .rect()
                            .contains(win.project_path.geometry())
                        ), "Project path overflows its card"
                    if captures and not motion:
                        assert win.grab().save(str(captures / f"page-{i}-{width}.png"))
        # Preview only synthetic host metadata; no worker, browser, grant or
        # external connection is started to produce these images.
        if captures:
            from unittest.mock import patch

            from .desktop_control import pause_local

            host = dict(
                worker_ready=True,
                enabled=True,
                emergency_hotkey_available=True,
                resources=dict(
                    cpu_percent=12,
                    ram_percent=38,
                    available_mib=8192,
                    admission_ready=True,
                ),
                sessions=[
                    dict(
                        kind="browser",
                        session_id="b" * 32,
                        task="Synthetic preview",
                        tabs=2,
                        presentation="background",
                    )
                ],
            )
            win.resize(1220, 820)
            win.navigate(5)
            with patch("udb.host_client.status", return_value=host):
                for state in ("ready", "owner", "paused"):
                    host["sessions"] = (
                        []
                        if state == "ready"
                        else [
                            dict(
                                kind="browser",
                                session_id="b" * 32,
                                task="Synthetic preview",
                                tabs=2,
                                presentation="background",
                            )
                        ]
                    )
                    if host["sessions"]:
                        host["sessions"][0]["presentation"] = (
                            "visible" if state == "owner" else "background"
                        )
                    pause_local(ctl.agent, state == "paused")
                    win.refresh()
                    QTest.qWait(60)
                    assert win.grab().save(str(captures / f"assistant-{state}.png"))
                pause_local(ctl.agent, False)

        # The compact view uses only metadata from this disposable owner home.
        import time

        from .activity import Activity
        from .desktop_control import paused

        win.refresh()
        win.open_quick_controls()
        quick = win.quick_controls
        quick.resize(480, 700)
        app.processEvents()
        writer = Activity(ctl.agent.home)
        writer.emit(
            "call", "host_browser", "quick-preview", details={"action": "select_option"}
        )
        deadline = time.monotonic() + 5
        while not quick.events and time.monotonic() < deadline:
            quick.poll_activity()
            QTest.qWait(20)
        assert quick.history.topLevelItemCount() == 1
        assert "Chọn dropdown" in quick.activity_detail.text()
        for button in (
            quick.pause_button,
            quick.resume_button,
            quick.handoff_button,
            quick.stop_button,
        ):
            assert button.isVisible()
            assert quick.rect().contains(button.mapTo(quick, button.rect().center()))
        quick.resize(480, 560)
        app.processEvents()
        assert quick.height() <= 560
        for button in (
            quick.pause_button,
            quick.resume_button,
            quick.handoff_button,
            quick.stop_button,
            quick.expand_button,
        ):
            assert quick.rect().contains(button.mapTo(quick, button.rect().center()))
        if captures:
            assert quick.grab().save(str(captures / "quick-small.png"))
        quick.resize(480, 700)
        quick.pause_button.click()
        assert quick.resume_button.isEnabled() and paused(ctl.agent)
        if captures:
            assert quick.grab().save(str(captures / "quick-paused.png"))
        quick.resume_button.click()
        assert not paused(ctl.agent)
        if captures:
            assert quick.grab().save(str(captures / "quick-activity.png"))
        # Actual native tray registration is tested when the CI desktop supports it.
        if quick.install_tray():
            assert win.close() is False and not win.isVisible()
            assert not ctl.closed.is_set()
            assert quick.close() is False
            quick.tray.hide()
            win._keep_in_tray = False
            app.setQuitOnLastWindowClosed(True)
        quick.hide()
        win.show()
        assert not (ctl.agent.state / "computer-access.dpapi").exists()

        # Empty states hide their corresponding tables and disable review.
        # Expire only the synthetic request in this disposable test database.
        with ctl.agent.connect() as c:
            c.execute("UPDATE requests SET created=0")
        win.refresh()
        win.navigate(1)
        QTest.qWait(100)
        assert win.approval_empty.isVisible() and win.requests.isHidden()
        assert not win.approval_review.isEnabled()
        if captures:
            assert win.grab().save(str(captures / "approvals-empty.png"))
        assert not (source / "gui-proof.py").exists()
        win.close()
        from PySide6.QtCore import QThreadPool

        assert QThreadPool.globalInstance().waitForDone(2000)
        app.processEvents()
        ctl.close()
