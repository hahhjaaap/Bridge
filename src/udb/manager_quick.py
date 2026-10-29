"""Compact local controls and a discoverable tray entry; no new permissions."""

import html
import time

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHeaderView,
    QMenu,
    QSystemTrayIcon,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .activity_presenter import ActivityFeed, describe, event_state, summary
from .manager_widgets import (
    ACCENT,
    TEXT,
    WARNING,
    MotionButton,
    StatusPill,
    Worker,
    icon,
    label,
    panel,
    row,
)


class QuickControls(QDialog):
    def __init__(self, owner):
        super().__init__(owner, Qt.WindowType.Window)
        self.owner = owner
        self.tray = None
        self.worker = None
        self.feed = ActivityFeed(owner.agent.home)
        self.events = []
        self.setWindowTitle("UDB · Điều khiển nhanh")
        self.setWindowIcon(icon("brand", ACCENT, 32))
        self.resize(480, 670)
        self.setMinimumSize(430, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)
        self.connection = StatusPill()
        lay.addLayout(row(label("UDB", "Brand"), None, self.connection))
        lay.addWidget(
            label(
                "Giao việc trong ChatGPT. Giữ quyền điều khiển tại đây.", "Fine", True
            )
        )
        self.connect_button = MotionButton("Kết nối", "power")
        self.connect_button.clicked.connect(owner.toggle_tunnel)
        self.settings_button = MotionButton("Cài đặt", "settings")
        self.settings_button.clicked.connect(owner.configure)
        lay.addLayout(row(self.connect_button, self.settings_button))

        card = panel()
        box = QVBoxLayout(card)
        box.setContentsMargins(16, 14, 16, 14)
        box.setSpacing(10)
        self.control_state = StatusPill()
        box.addLayout(row(label("ĐIỀU KHIỂN", "Eyebrow"), None, self.control_state))
        self.control_hint = label("", "Fine", True)
        box.addWidget(self.control_hint)
        self.pause_button = MotionButton("Tạm dừng Windows", "pause", "danger")
        self.pause_button.clicked.connect(lambda: owner.pause_desktop_local(True))
        self.pause_button.setToolTip(
            "Tạm dừng thao tác Windows. Browser nền vẫn có thể tiếp tục theo quyền đã cấp."
        )
        self.resume_button = MotionButton("Tiếp tục Windows", "play")
        self.resume_button.clicked.connect(lambda: owner.pause_desktop_local(False))
        box.addLayout(row(self.pause_button, self.resume_button))
        self.handoff_button = MotionButton("Tôi đăng nhập browser", "browser")
        self.handoff_button.clicked.connect(self.handoff)
        self.stop_button = MotionButton("Đóng phiên nền", "power", "danger")
        self.stop_button.clicked.connect(owner.stop_host_local)
        self.stop_button.setToolTip(
            "Tắt cho phép mở phiên nền và đóng browser/ghi chú do UDB mở."
        )
        box.addLayout(row(self.handoff_button, self.stop_button))
        lay.addWidget(card)

        self.activity_title = label("Chưa có hoạt động", "H2", True)
        self.activity_detail = label("", "Muted", True)
        lay.addWidget(self.activity_title)
        lay.addWidget(self.activity_detail)
        self.history = QTreeWidget()
        self.history.setHeaderLabels(["Hoạt động gần đây", "Kết quả"])
        self.history.setRootIsDecorated(False)
        self.history.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.history.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.history.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.history.setMinimumHeight(105)
        self.history.setMaximumHeight(170)
        lay.addWidget(self.history, 1)
        self.activity_note = label(
            "Nhật ký lời gọi công cụ; không phải tiến độ toàn bộ công việc.",
            "Fine",
            True,
        )
        lay.addWidget(self.activity_note)
        self.visibility_hint = label(
            "Đóng bảng này để trở về cửa sổ UDB.", "Fine", True
        )
        lay.addWidget(self.visibility_hint)
        advanced = MotionButton("Mở UDB đầy đủ", "external")
        advanced.clicked.connect(lambda: self.show_manager(5))
        lay.addWidget(advanced)
        self.sync()

    def install_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return False
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu(self)
        menu.addAction("Điều khiển nhanh", self.show_panel)
        menu.addAction("Mở UDB đầy đủ", lambda: self.show_manager(5))
        menu.addSeparator()
        menu.addAction("Tạm dừng Windows", lambda: self.owner.pause_desktop_local(True))
        menu.addAction("Đóng phiên nền", self.owner.stop_host_local)
        menu.addSeparator()
        menu.addAction("Thoát UDB", self.request_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.activated)
        self.tray.show()
        self.owner._keep_in_tray = True
        QApplication.instance().setQuitOnLastWindowClosed(False)
        self.visibility_hint.setText(
            "Đóng cửa sổ để thu về khay hệ thống. Chọn Thoát UDB trong menu để dừng ứng dụng."
        )
        self.sync()
        return True

    def activated(self, reason):
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_panel()

    def show_panel(self):
        self.sync()
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.poll_activity()

    def show_manager(self, page):
        self.owner.navigate(page)
        self.owner.showNormal()
        self.owner.raise_()
        self.owner.activateWindow()
        self.hide()

    def handoff(self):
        self.owner.browser_presentation_local(not self.owner._browser_handed_off)

    def sync(self, status_error=False):
        owner = self.owner
        self.connection.set_status(
            owner.status_label.text().removeprefix("●  "),
            "ready"
            if getattr(owner, "_connection_ready", False) and not status_error
            else "neutral",
        )
        self.connect_button.setText(owner.connect_button.text())
        self.connect_button.setEnabled(owner.connect_button.isEnabled())
        self.settings_button.setEnabled(not owner.busy)
        text, tone = owner.desktop_state_pill._status
        self.control_state.set_status(
            "Trạng thái chưa cập nhật" if status_error else text,
            "neutral" if status_error else tone,
        )
        self.control_hint.setText(
            "Không lấy được trạng thái mới. Nút tạm dừng Windows vẫn dùng được."
            if status_error
            else owner.desktop_pause_status.text()
        )
        self.pause_button.setEnabled(owner.desktop_pause_button.isEnabled())
        self.pause_button.setToolTip(
            "Tạm dừng thao tác Windows. Browser nền vẫn theo quyền đã cấp.\n"
            + owner.desktop_hotkey_hint.text()
        )
        self.resume_button.setEnabled(owner.desktop_resume_button.isEnabled())
        handed_off = getattr(owner, "_browser_handed_off", False)
        self.handoff_button.setText(
            "Trả về cho AI" if handed_off else "Tôi đăng nhập browser"
        )
        self.handoff_button.setEnabled(
            not owner.busy
            and not status_error
            and (
                owner.browser_background_button.isEnabled()
                if handed_off
                else owner.browser_login_button.isEnabled()
            )
        )
        self.stop_button.setEnabled(owner.host_stop_button.isEnabled())
        if self.tray:
            self.tray.setToolTip(
                "UDB · " + self.control_state.text().removeprefix("●  ")
            )
        self.render_activity()
        if self.isVisible():
            self.poll_activity()

    def poll_activity(self):
        if self.worker is not None or self.owner._closing:
            return
        worker = Worker(self.feed.poll)
        self.worker = worker

        def done(events):
            self.worker = None
            self._activity_unavailable = False
            self.events = events
            self.activity_note.setText(
                "Nhật ký lời gọi công cụ; không phải tiến độ toàn bộ công việc."
            )
            self.render_activity()

        def failed(_message):
            self.worker = None
            self._activity_unavailable = True
            self.activity_note.setText(
                "Chưa cập nhật được nhật ký. UDB sẽ tự đọc lại; các nút điều khiển vẫn dùng được."
            )
            self.render_activity()

        worker.signals.done.connect(done, Qt.ConnectionType.QueuedConnection)
        worker.signals.error.connect(failed, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(worker)

    def render_activity(self):
        title, detail, tone = summary(self.events)
        if getattr(self, "_activity_unavailable", False):
            title, detail = (
                "Nhật ký chưa cập nhật",
                "Chưa xác nhận được hoạt động mới nhất.",
            )
            tone = "paused"
        self.activity_title.setText(title)
        self.activity_title.setStyleSheet(
            "color:" + (WARNING if tone == "paused" else TEXT)
        )
        self.activity_detail.setText(detail)
        signature = tuple((x["seq"], x.get("status")) for x in self.events)
        if signature == getattr(self, "_history_signature", None):
            return
        self._history_signature = signature
        self.history.clear()
        self.history.setVisible(bool(self.events))
        for event in self.events[:8]:
            item = QTreeWidgetItem([describe(event), event_state(event)])
            item.setToolTip(
                0,
                "<qt>"
                + html.escape(
                    time.strftime("%H:%M:%S", time.localtime(event["at"]))
                    + " · "
                    + describe(event)
                )
                + "</qt>",
            )
            self.history.addTopLevelItem(item)

    def request_quit(self):
        self.owner._quit_requested = True
        if not self.owner.close():
            self.owner._quit_requested = False
            self.show_panel()
            self.visibility_hint.setText(
                "Chưa thể thoát. Chờ thao tác hiện tại hoàn tất rồi chọn Thoát UDB lại."
            )
            return False
        if self.tray:
            self.tray.hide()
        self.hide()
        QApplication.instance().quit()
        return True

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self.tray and not self.owner._closing:
            self.hide()
            event.ignore()
        else:
            if not self.owner._closing:
                self.owner.showNormal()
            event.accept()
