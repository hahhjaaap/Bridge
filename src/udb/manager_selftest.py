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
        app.processEvents()
        ctl.close()
