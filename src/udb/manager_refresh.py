"""Project/status refresh logic for the UDB desktop manager."""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem

from .manager_widgets import ACCENT, MUTED, RAISED, WARNING, icon


class ManagerRefreshMixin:
    def refresh(self):
        if self._closing:
            return
        try:
            source_auto_projects = self._refresh_projects()
            self._refresh_host()
            self._refresh_requests()
            self._refresh_jobs()
            self._refresh_connection(source_auto_projects)
        except Exception as e:
            self.footer_status.setText("Không tải được trạng thái: " + str(e))

    def _refresh_projects(self):
        projects = self.agent.projects()
        source_auto_projects = set(self.agent.source_autonomy_projects())
        signature = json.dumps(projects)
        if signature != self.last_signature:
            self.last_signature = signature
            selected = self.selected_project["id"] if self.selected_project else None
            self.projects.clear()
            for proj in projects:
                item = QTreeWidgetItem(
                    [
                        proj["id"],
                        "AI tự sửa"
                        if proj["id"] in source_auto_projects
                        else "Ghi có giới hạn"
                        if proj["writable"]
                        else "Chỉ đọc",
                    ]
                )
                item.setIcon(0, icon("folder", MUTED))
                item.setData(0, Qt.ItemDataRole.UserRole, proj)
                item.setToolTip(0, proj["root"])
                self.projects.addTopLevelItem(item)
                if proj["id"] == selected:
                    self.projects.setCurrentItem(item)
            if not self.projects.selectedItems() and projects:
                self.projects.setCurrentItem(self.projects.topLevelItem(0))
            self.project_selected()
        elif time.monotonic() - self._last_project_auth_check >= 10.0:
            self.project_selected()
        return source_auto_projects

    def _refresh_host(self):
        from .desktop_control import paused
        from .host_client import status as host_status

        hs = host_status(self.agent)
        rs = hs["resources"]
        is_paused = paused(self.agent)
        sessions = hs.get("sessions", [])
        handed_off = any(x.get("presentation") == "visible" for x in sessions)
        ready = hs.get("worker_ready") is True
        enabled = hs.get("enabled") is True
        if is_paused:
            tone, state = "paused", "Đã tạm dừng"
            title = "Điều khiển Windows đang tạm dừng"
            hint = "Bạn có thể tiếp tục bất cứ lúc nào. Các phiên nền vẫn làm việc theo quyền đã cấp."
        elif handed_off:
            tone, state = "owner", "Bạn điều khiển"
            title = "Browser đang được nhường cho bạn"
            hint = "Đăng nhập trực tiếp trong cửa sổ UDB. Bấm Trả về cho AI khi bạn đã xong."
        elif not ready:
            tone, state = "neutral", "Chưa sẵn sàng"
            title = "Đang chờ tác nhân UDB"
            hint = "Trạng thái sẽ cập nhật khi tác nhân cục bộ sẵn sàng. Bạn vẫn có thể tạm dừng Windows."
        elif not enabled:
            tone, state = "neutral", "Chưa bật phiên nền"
            title = "Bạn chọn khi nào AI được chạy nền"
            hint = "Bật cho phép bên dưới để AI mở browser riêng khi bạn giao việc."
        elif not rs.get("admission_ready"):
            tone, state = "paused", "Đang chờ tài nguyên"
            title = "UDB đang chờ máy bớt bận"
            hint = "Chưa mở thêm phiên mới. Các nút tạm dừng và đóng phiên luôn ở đây."
        else:
            tone, state = "ready", "Sẵn sàng chạy nền"
            title = "Bạn luôn nắm quyền điều khiển"
            hint = "AI ưu tiên chạy nền. Khi dùng desktop, UDB nhường nếu bạn thao tác."
        self.desktop_state_pill.set_status(state, tone)
        self.desktop_state_title.setText(title)
        self.desktop_pause_status.setText(hint)
        self.desktop_pause_button.setEnabled(not is_paused)
        self.desktop_resume_button.setEnabled(is_paused)
        self.desktop_hotkey_hint.setText(
            "Tạm dừng nhanh · Ctrl + Alt + Shift + Backspace"
            if hs.get("emergency_hotkey_available")
            else "Tạm dừng bằng nút ở trên · Phím tắt chưa sẵn sàng"
        )
        self.host_enabled.blockSignals(True)
        self.host_enabled.setChecked(enabled)
        self.host_enabled.blockSignals(False)
        capacity = rs.get("admission_ready") is True
        self.browser_start_button.setEnabled(
            ready
            and enabled
            and capacity
            and not any(x["kind"] == "browser" for x in sessions)
        )
        self.browser_login_button.setEnabled(
            ready and enabled and capacity and not handed_off
        )
        self.browser_background_button.setEnabled(ready and enabled and handed_off)
        self.workspace_start_button.setEnabled(
            ready
            and enabled
            and capacity
            and not any(x["kind"] == "workspace" for x in sessions)
        )
        browser_open = any(x["kind"] == "browser" for x in sessions)
        self.browser_remember.setEnabled(ready and not browser_open)
        self.browser_remember.setToolTip(
            "Đóng browser trước khi đổi lựa chọn nhớ đăng nhập."
            if browser_open
            else "Chỉ nhớ đăng nhập trong browser riêng của UDB."
        )
        self.host_stop_button.setEnabled(enabled or bool(sessions))
        cpu, ram = rs.get("cpu_percent"), rs.get("ram_percent")
        self.host_resource.setText(
            f"CPU {cpu if cpu is not None else '—'}%  ·  RAM {ram if ram is not None else '—'}%  ·  Trống {rs.get('available_mib', 0)} MB"
        )
        self.host_pressure.setText(
            "Đủ tài nguyên để mở phiên."
            if rs.get("admission_ready")
            else "Máy đang bận hoặc chưa có số liệu mới; UDB đang chờ."
        )
        signature = tuple(
            (
                x["session_id"],
                x["kind"],
                x.get("presentation"),
                x.get("tabs"),
                x.get("task", ""),
            )
            for x in sessions
        )
        if signature != getattr(self, "_host_sessions_signature", None):
            self._host_sessions_signature = signature
            self.host_sessions.clear()
            for session in sessions:
                browser = session["kind"] == "browser"
                item = QTreeWidgetItem(
                    [
                        "Browser riêng" if browser else "Không gian ghi chú",
                        "Bạn điều khiển"
                        if session.get("presentation") == "visible"
                        else "Chạy nền",
                        str(session.get("tabs", 0)) if browser else "—",
                    ]
                )
                item.setIcon(0, icon("browser" if browser else "file", ACCENT))
                item.setToolTip(
                    0, session.get("task", "") + "\n" + session["session_id"]
                )
                self.host_sessions.addTopLevelItem(item)
        self.host_sessions.setVisible(bool(sessions))
        self.host_sessions_empty.setVisible(not sessions)

    def _refresh_requests(self):
        self.media_auto.blockSignals(True)
        self.media_auto.setChecked(self.agent.media_autonomy())
        self.media_auto.blockSignals(False)
        rows = self.agent.pending_local()
        self.pending_rows = {r["id"]: r for r in rows}
        signature = tuple(self.pending_rows)
        if signature != self.last_pending:
            self.last_pending = signature
            self.requests.clear()
            for request in rows:
                p = json.loads(request["payload"])
                item = QTreeWidgetItem(
                    [
                        p["project"]
                        + " / "
                        + p.get("path", p.get("task", p.get("language", ""))),
                        "Sửa file"
                        if request["kind"] == "write"
                        else "Chuột/phím"
                        if request["kind"] == "desktop"
                        else "Sandbox",
                        "",
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, request["id"])
                self.requests.addTopLevelItem(item)
            if rows:
                self.requests.setCurrentItem(self.requests.topLevelItem(0))
            if rows:
                p = json.loads(rows[0]["payload"])
                self.pending_title.setText(
                    p.get("path", p.get("language", "Phiên chuột/phím"))
                )
                self.pending_hint.setText(
                    p["project"]
                    + " · "
                    + (
                        "Thay đổi file"
                        if rows[0]["kind"] == "write"
                        else "Chuột/phím trong Sandbox"
                        if rows[0]["kind"] == "desktop"
                        else "Tải video FFmpeg"
                        if rows[0]["kind"] == "media"
                        else "Chạy trong Sandbox"
                    )
                )
                self.pending_code.setPlainText(
                    p.get("diff", p.get("script", p.get("task", "")))[:8000]
                )
            else:
                self.pending_title.setText("Không có yêu cầu chờ duyệt")
                self.pending_hint.setText(
                    "Đề xuất sửa và chạy mã từ Chat sẽ xuất hiện tại đây."
                )
                self.pending_code.clear()
        for i, r in enumerate(rows):
            self.requests.topLevelItem(i).setText(
                2, str(max(0, int(r["created"] + 600 - time.time()))) + " giây"
            )
        self.pending_code.setVisible(bool(rows))
        self.requests.setVisible(bool(rows))
        self.approval_empty.setVisible(not rows)
        self.approval_review.setEnabled(bool(rows))
        self.review_button.setEnabled(bool(rows))
        self.nav_buttons[1].setText("Quyền AI" + (f"  {len(rows)}" if rows else ""))

    def _refresh_jobs(self):
        with self.agent.connect() as c:
            protected = [
                r[0] for r in c.execute("SELECT root FROM protected ORDER BY root")
            ]
            jobs = [
                dict(r)
                for r in c.execute("SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30")
            ]
        if self.protected.topLevelItemCount() != len(protected):
            self.protected.clear()
            for path in protected:
                self.protected.addTopLevelItem(QTreeWidgetItem([path]))
        job_sig = json.dumps(jobs)
        if job_sig != self.last_job_signature:
            self.last_job_signature = job_sig
            current = self.jobs.currentItem()
            old = current.data(0, Qt.ItemDataRole.UserRole) if current else None
            self.jobs.clear()
            for j in jobs:
                info = json.loads(j["info"])
                status_text = {
                    "running": "Đang chạy",
                    "reviewing": "Đã xong · đang xem",
                    "completed": "Hoàn tất",
                    "failed": "Lỗi",
                    "timeout": "Hết thời gian",
                    "output_limit": "Log vượt giới hạn",
                    "stopping": "Đang dừng",
                    "stopped": "Đã dừng",
                }.get(j["status"], j["status"])
                item = QTreeWidgetItem(
                    [
                        info.get("project", ""),
                        info.get(
                            "language",
                            "Desktop" if info.get("kind") == "desktop" else "",
                        ),
                        status_text,
                        j["id"],
                    ]
                )
                item.setData(0, Qt.ItemDataRole.UserRole, j["id"])
                self.jobs.addTopLevelItem(item)
                if j["id"] == old:
                    self.jobs.setCurrentItem(item)
            if jobs and not self.jobs.selectedItems():
                self.jobs.setCurrentItem(self.jobs.topLevelItem(0))
        self.jobs.setVisible(bool(jobs))
        self.job_tabs.setVisible(bool(jobs))
        self.jobs_empty.setVisible(not jobs)
        if self.jobs.selectedItems():
            self.show_job()

    def _refresh_connection(self, source_auto_projects):
        status = self.controller.status()
        ready = status["ready"]
        alive = status["tunnel_running"]
        wanted = status["tunnel_wanted"]
        text = (
            "Đã sẵn sàng"
            if ready
            else (
                "Đang kết nối…"
                if alive or status["connecting"]
                else f"Thử lại sau {status['retry_in_seconds']:.0f} giây"
                if wanted
                else "Chưa kết nối"
            )
        )
        if not self.busy:
            self.status_label.setText("●  " + text)
        self.status_label.setStyleSheet(
            "color:"
            + (ACCENT if ready else WARNING if alive else MUTED)
            + "; background:"
            + RAISED
            + "; border-radius:9px; padding:4px 10px;"
        )
        self.connect_button.setText("Ngắt kết nối" if wanted else "Kết nối")
        self.connect_button.setEnabled(not self.busy)
        self.local_button.setEnabled(not self.busy)
        self.local_button.setText(
            "Tắt MCP cục bộ" if status["local_running"] else "Bật MCP cục bộ"
        )
        self.connection_hint.setText(
            "Sẵn sàng nhận yêu cầu từ ChatGPT Chat."
            if ready
            else "Private MCP tunnel · không mở cổng công khai"
        )
        self.detail_status.setText(
            "Tunnel: "
            + text
            + "\nMCP cục bộ: "
            + ("đang bật" if status["local_running"] else "đang tắt")
            + "\nKhóa: lưu trong Windows Credential Manager · không hiển thị"
        )
        setup = self.agent.state / "sandbox-setup-result.json"
        needs_restart = False
        if setup.exists():
            try:
                needs_restart = bool(
                    json.loads(setup.read_text("utf-8-sig")).get("RestartNeeded")
                )
            except (OSError, ValueError):
                pass
        self.sandbox_label.setText(
            (
                "Sandbox: chưa sẵn sàng. Lần bật tính năng trước yêu cầu khởi động lại Windows."
                if needs_restart
                else "Sandbox chưa khả dụng. Chạy mã đang bị khóa."
            )
            if not status["sandbox"]
            else "Sandbox: thành phần đã cài, chờ kiểm thử chạy mã."
        )
        sig = (ready, alive)
        if sig != self.connection_signature:
            self.connection_signature = sig
            self.band.flash()
        self.footer_status.setText(
            ("Tunnel sẵn sàng" if ready else "Chờ kết nối")
            + " · "
            + ("AI tự sửa theo quyền" if source_auto_projects else "Chưa bật tự sửa")
            + " · Quyền do bạn cấp"
        )
