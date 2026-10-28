"""Local host/browser controls for the native UDB manager.

Page composition and owner-only actions live here; readiness rendering remains
in manager_refresh. No remote tool can invoke the local permission setters.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QHeaderView,
    QScrollArea,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .manager_widgets import (
    MotionButton,
    ResponsiveRow,
    StatusPill,
    label,
    panel,
    row,
)


class ManagerHostMixin:
    def build_host(self):
        p, lay = self.page(
            "Trợ lý trên máy",
            "Giao việc trong ChatGPT. Theo dõi và giữ quyền điều khiển tại đây.",
        )
        hero = panel()
        hero.setObjectName("Hero")
        hl = QVBoxLayout(hero)
        hl.setContentsMargins(20, 18, 20, 18)
        hl.setSpacing(10)
        self.desktop_state_pill = StatusPill("Đang kiểm tra")
        hl.addLayout(
            row(label("QUYỀN ĐIỀU KHIỂN", "Eyebrow"), None, self.desktop_state_pill)
        )
        self.desktop_state_title = label("Bạn luôn có thể tạm dừng", "H2", True)
        hl.addWidget(self.desktop_state_title)
        self.desktop_pause_status = label("Đang lấy trạng thái…", "Muted", True)
        hl.addWidget(self.desktop_pause_status)
        self.desktop_pause_button = MotionButton("Tạm dừng Windows", "pause", "danger")
        self.desktop_pause_button.clicked.connect(
            lambda: self.pause_desktop_local(True)
        )
        self.desktop_resume_button = MotionButton("Tiếp tục", "play")
        self.desktop_resume_button.clicked.connect(
            lambda: self.pause_desktop_local(False)
        )
        self.desktop_resume_button.setEnabled(False)
        hl.addLayout(row(self.desktop_pause_button, self.desktop_resume_button, None))
        lay.addWidget(hero)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(140)
        content = QWidget()
        content.setObjectName("Page")
        cards = QVBoxLayout(content)
        cards.setContentsMargins(0, 0, 8, 0)
        cards.setSpacing(14)

        browser_card = panel()
        bl = QVBoxLayout(browser_card)
        bl.setContentsMargins(20, 18, 20, 18)
        bl.setSpacing(10)
        bl.addLayout(
            row(
                label("Trình duyệt riêng", "H2"),
                None,
                StatusPill("UDB · AI", "neutral"),
            )
        )
        bl.addWidget(
            label(
                "AI làm việc trong browser riêng, không giành chuột hay tab cá nhân.",
                "Muted",
                True,
            )
        )
        self.host_enabled = QCheckBox("Cho phép mở phiên nền trên máy này")
        self.host_enabled.toggled.connect(self.set_host_enabled)
        bl.addWidget(self.host_enabled)
        self.browser_start_button = MotionButton("Mở browser nền", "browser", "primary")
        self.browser_start_button.clicked.connect(
            lambda: self.start_host_local("browser")
        )
        self.browser_login_button = MotionButton("Tôi đăng nhập", "external")
        self.browser_login_button.clicked.connect(
            lambda: self.browser_presentation_local(True)
        )
        self.browser_background_button = MotionButton("Trả về cho AI", "arrow")
        self.browser_background_button.clicked.connect(
            lambda: self.browser_presentation_local(False)
        )
        bl.addLayout(row(self.browser_start_button, None))
        bl.addLayout(
            row(self.browser_login_button, self.browser_background_button, None)
        )
        from .browser_settings import remember_login

        self.browser_remember = QCheckBox("Nhớ đăng nhập trong browser UDB")
        self.browser_remember.setChecked(remember_login(self.agent))
        self.browser_remember.toggled.connect(self.set_browser_remember_local)
        bl.addWidget(self.browser_remember)
        bl.addWidget(
            label(
                "Khi bạn đăng nhập, AI nhường quyền. Đổi chế độ sẽ tải lại trang; hãy lưu biểu mẫu trước. Đóng browser trước khi đổi lựa chọn nhớ đăng nhập; bỏ chọn sẽ xóa đăng nhập UDB đã lưu.",
                "Fine",
                True,
            )
        )

        sessions = panel()
        sl = QVBoxLayout(sessions)
        sl.setContentsMargins(20, 18, 20, 18)
        sl.setSpacing(10)
        workspace = MotionButton("Mở ghi chú", "file")
        workspace.clicked.connect(lambda: self.start_host_local("workspace"))
        self.workspace_start_button = workspace
        sl.addLayout(row(label("Phiên đang mở", "H2"), None, workspace))
        self.host_sessions = self.tree(["Phiên", "Trạng thái", "Trang"])
        self.host_sessions.setMinimumHeight(110)
        self.host_sessions.setMaximumHeight(175)
        self.host_sessions.header().setStretchLastSection(False)
        self.host_sessions.header().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.host_sessions.header().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.host_sessions.header().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.host_sessions_empty = label(
            "Chưa có phiên đang mở.\nAI có thể mở phiên khi bạn giao việc và đã bật cho phép ở trên.",
            "Muted",
            True,
        )
        self.host_sessions_empty.setMinimumHeight(62)
        self.host_sessions_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sl.addWidget(self.host_sessions_empty)
        sl.addWidget(self.host_sessions)
        sl.addStretch()
        cards.addWidget(ResponsiveRow(browser_card, sessions))

        details = QToolButton()
        details.setText("Chi tiết tài nguyên")
        details.setCheckable(True)
        details.setArrowType(Qt.ArrowType.RightArrow)
        details.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        details.setCursor(Qt.CursorShape.PointingHandCursor)
        cards.addWidget(details)
        detail_body = QWidget()
        dl = QVBoxLayout(detail_body)
        dl.setContentsMargins(0, 0, 0, 0)
        self.host_resource = label("Đang lấy số liệu…", "Fine", True)
        self.host_pressure = label("", "Fine", True)
        dl.addWidget(self.host_resource)
        dl.addWidget(self.host_pressure)
        dl.addWidget(
            label(
                "Tối đa 2 phiên riêng · 4 tab browser. UDB chờ khi máy thiếu tài nguyên.",
                "Fine",
                True,
            )
        )
        detail_body.hide()

        def toggle_details(checked):
            detail_body.setVisible(checked)
            details.setArrowType(
                Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
            )

        details.toggled.connect(toggle_details)
        cards.addWidget(detail_body)
        cards.addStretch()
        scroll.setWidget(content)
        lay.addWidget(scroll, 1)
        self.host_stop_button = MotionButton("Tắt phiên nền", "power", "danger")
        self.host_stop_button.setToolTip(
            "Tắt cho phép mở phiên nền và đóng các phiên UDB hiện có."
        )
        self.host_stop_button.clicked.connect(self.stop_host_local)
        self.desktop_hotkey_hint = label("Tạm dừng bằng nút ở trên", "Fine", True)
        lay.addLayout(
            row(
                self.desktop_hotkey_hint,
                None,
                self.host_stop_button,
            )
        )
        return p

    def set_browser_remember_local(self, value):
        from .browser_settings import remember_login, set_remember_login_local

        self.guarded(lambda: set_remember_login_local(self.agent, value))
        self.browser_remember.blockSignals(True)
        self.browser_remember.setChecked(remember_login(self.agent))
        self.browser_remember.blockSignals(False)

    def browser_presentation_local(self, visible):
        from .host_client import command

        self.run_background(
            lambda: command(self.agent, "browser_presentation", visible=visible)
        )

    def pause_desktop_local(self, value):
        from .desktop_control import pause_local

        pause_local(self.agent, value)
        self.refresh()

    def set_host_enabled(self, value):
        from .host_client import atomic_json

        self.guarded(
            lambda: atomic_json(
                self.agent.state / "host-settings.json", {"enabled": bool(value)}
            )
        )

    def start_host_local(self, kind):
        from .host_client import command

        self.run_background(
            lambda: command(
                self.agent,
                "start",
                kind=kind,
                task="Phiên riêng do chủ máy mở trong UDB",
                duration_seconds=900,
            )
        )

    def stop_host_local(self):
        # Local revocation is immediate for new commands. The monitor closes
        # only jobs owned by this engine, even if an action is in flight.
        self.host_enabled.setChecked(False)
