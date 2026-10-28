"""UDB native Qt UI. Host approvals and Sandbox autonomy grants stay local."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    QLockFile,
    Qt,
    QThreadPool,
    QTimer,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyleFactory,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .core import reject_links
from .desktop import DesktopController
from .manager_dialogs import ApprovalDialog, ConnectionDialog, SourceDialog
from .manager_refresh import ManagerRefreshMixin
from .manager_widgets import (
    ACCENT,
    BG,
    PANEL,
    STYLE,
    TEXT,
    DiffHighlighter,
    MotionButton,
    ResponsiveRow,
    StatusBand,
    StatusPill,
    Worker,
    empty_state,
    icon,
    label,
    panel,
    row,
)

PLUGIN_URL = (
    "https://chatgpt.com/plugins/plugin_asdk_app_6ad78a6c547c81919d4efa885507ead2"
)


class MainWindow(ManagerRefreshMixin, QMainWindow):
    def __init__(self, home, controller=None):
        super().__init__()
        self.controller = controller or DesktopController(Path(home))
        self.agent = self.controller.agent
        self.busy = False
        self.worker = None
        self.workers = []
        self.pending_rows = {}
        self.selected_project = None
        self.last_signature = None
        self.last_pending = None
        self.last_job_signature = None
        self.connection_signature = None
        self._closing = False
        self._last_project_auth_check = 0.0
        self.setWindowTitle("Universal Desktop Bridge · " + __version__)
        self.resize(1220, 820)
        self.setMinimumSize(940, 640)
        self.setWindowIcon(icon("brand", ACCENT, 32))
        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)
        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(200)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(16, 26, 16, 20)
        nav.setSpacing(9)
        nav.addWidget(label("UDB", "Brand"))
        nav.addWidget(label("TRỢ LÝ TRÊN MÁY", "Eyebrow"))
        nav.addSpacing(26)
        self.nav_buttons = []
        for i, (text, glyph) in enumerate(
            (
                ("Dự án", "projects"),
                ("Quyền AI", "review"),
                ("Tiến trình", "activity"),
                ("Bảo vệ", "shield"),
                ("Kết nối", "settings"),
                ("Máy tính", "cursor"),
            )
        ):
            btn = MotionButton(text, glyph, "nav")
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda checked=False, index=i: self.navigate(index))
            nav.addWidget(btn)
            self.nav_buttons.append(btn)
        nav.addStretch()
        protected = label("Bạn quyết định\nAI được làm gì", "Muted", True)
        nav.addWidget(protected)
        nav.addSpacing(12)
        nav.addWidget(label("DESKTOP BRIDGE", "Eyebrow"))
        nav.addWidget(label("Agent " + __version__, "Fine"))
        body.addWidget(sidebar)
        self.stack = QStackedWidget()
        body.addWidget(self.stack, 1)
        self.dashboard = self.build_dashboard()
        self.stack.addWidget(self.dashboard)
        self.stack.addWidget(self.build_approvals())
        self.stack.addWidget(self.build_jobs())
        self.stack.addWidget(self.build_protection())
        self.stack.addWidget(self.build_connection())
        self.stack.addWidget(self.build_host())
        foot = QFrame()
        foot.setObjectName("Footer")
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(22, 9, 22, 9)
        self.footer_status = label("MCP riêng tư · Quyền tự động theo dự án", "Fine")
        fl.addWidget(self.footer_status)
        fl.addStretch()
        self.footer_project = label("Chưa chọn dự án", "Fine")
        fl.addWidget(self.footer_project)
        outer.addWidget(foot)
        # Keep stacked pages opaque. A graphics effect on the entire stack can
        # retain stale page pixels on Windows when pages change or resize.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.navigate(5)
        self.refresh()

    def page(self, title, subtitle):
        p = QWidget()
        p.setObjectName("Page")
        layout = QVBoxLayout(p)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(16)
        header = QWidget()
        header.setObjectName("PageHeader")
        hl = QVBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        h = label(title, "H1", True)
        h.setMinimumHeight(40)
        hl.addWidget(h)
        hl.addWidget(label(subtitle, "Subtitle", True))
        header.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        layout.addWidget(header)
        return p, layout

    def tree(self, columns):
        w = QTreeWidget()
        w.setHeaderLabels(columns)
        w.setRootIsDecorated(False)
        w.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        w.setUniformRowHeights(True)
        w.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        w.header().setStretchLastSection(True)
        return w

    def build_dashboard(self):
        p, lay = self.page(
            "Không gian làm việc",
            "AI tự thao tác trong dự án đã cấp quyền. Checkpoint và dữ liệu nhạy cảm luôn bị chặn.",
        )
        top = row(label("DỰ ÁN / WINDOWS", "Eyebrow"), None)
        self.status_label = label("●  Chưa kết nối", "Badge")
        top.addWidget(self.status_label)
        lay.insertLayout(0, top)
        self.band = StatusBand()
        band = QHBoxLayout(self.band)
        band.setContentsMargins(22, 20, 22, 20)
        info = QVBoxLayout()
        info.setSpacing(6)
        info.addWidget(label("Kết nối với ChatGPT Chat", "H2"))
        self.connection_hint = label(
            "Private MCP tunnel · không mở cổng công khai", "Muted", True
        )
        info.addWidget(self.connection_hint)
        band.addLayout(info, 1)
        self.configure_button = MotionButton("Cấu hình", "settings")
        self.configure_button.clicked.connect(self.configure)
        band.addWidget(self.configure_button)
        self.connect_button = MotionButton("Kết nối", "power", "primary")
        self.connect_button.clicked.connect(self.toggle_tunnel)
        band.addWidget(self.connect_button)
        lay.addWidget(self.band)
        split = QSplitter()
        split.setChildrenCollapsible(False)
        lay.addWidget(split, 1)
        left = panel()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(18, 16, 18, 16)
        ll.setSpacing(12)
        add = MotionButton("Thêm dự án", "plus")
        add.clicked.connect(self.add_project)
        ll.addLayout(row(label("Dự án nguồn", "H2"), None, add))
        self.projects = self.tree(["Dự án", "Quyền truy cập"])
        self.projects.setMinimumHeight(110)
        self.projects.setColumnWidth(0, 180)
        self.projects.itemSelectionChanged.connect(self.project_selected)
        self.projects.itemDoubleClicked.connect(lambda *_: self.open_source())
        ll.addWidget(self.projects, 1)
        self.project_path = QLineEdit()
        self.project_path.setObjectName("ProjectPath")
        self.project_path.setReadOnly(True)
        self.project_path.setMinimumWidth(0)
        self.project_path.setPlaceholderText("Thư mục nguồn")
        ll.addWidget(self.project_path)
        self.project_access = label("Chọn dự án để xem quyền truy cập.", "Fine", True)
        ll.addWidget(self.project_access)
        self.sandbox_auto = QCheckBox("AI tự làm việc trong Sandbox")
        self.sandbox_auto.setToolTip(
            "Cho dự án này tự mở desktop và chạy script trong VM. Không cấp quyền ghi file gốc hoặc điều khiển host. Bỏ chọn để thu hồi và dừng phiên tự động."
        )
        self.sandbox_auto.toggled.connect(self.set_sandbox_autonomy)
        ll.addWidget(self.sandbox_auto)
        self.source_auto = QCheckBox("AI tự sửa mã nguồn (không duyệt từng file)")
        self.source_auto.setToolTip(
            "Chỉ trong dự án đã đăng ký có quyền ghi. Không sửa checkpoint, dữ liệu, bí mật, file cấu hình quan trọng. Mỗi lần sửa có backup + SHA256; có thể tắt ngay."
        )
        self.source_auto.toggled.connect(self.set_source_autonomy)
        ll.addWidget(self.source_auto)
        view = MotionButton("Xem nguồn", "file")
        view.clicked.connect(self.open_source)
        revoke = MotionButton("Thu hồi", "", "danger")
        revoke.clicked.connect(self.revoke)
        ll.addLayout(row(view, revoke, None))
        split.addWidget(left)
        rail = panel()
        rail.setMinimumWidth(236)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(18, 18, 18, 18)
        rl.setSpacing(13)
        rl.addWidget(label("AN TOÀN & NGOẠI LỆ", "Eyebrow"))
        self.pending_title = label("Không có yêu cầu chờ duyệt", "H2", True)
        rl.addWidget(self.pending_title)
        self.pending_hint = label(
            "Trong phạm vi đã cấp quyền, AI sửa tự động; trường hợp ngoại lệ hiện tại đây.",
            "Muted",
            True,
        )
        rl.addWidget(self.pending_hint)
        self.pending_code = QPlainTextEdit()
        self.pending_code.setReadOnly(True)
        self.pending_code.setMaximumHeight(155)
        self.pending_code.setPlaceholderText("Chưa có thay đổi mới.")
        rl.addWidget(self.pending_code)
        self.rail_highlight = DiffHighlighter(self.pending_code.document())
        self.review_button = MotionButton("Xem đề xuất", "review")
        self.review_button.clicked.connect(self.review_first)
        rl.addWidget(self.review_button)
        rl.addStretch()
        self.sandbox_label = label("", "Warning", True)
        rl.addWidget(self.sandbox_label)
        rl.addWidget(
            label(
                "Tự sửa mã nguồn đã cho phép; file quan trọng luôn bị chặn.",
                "Fine",
                True,
            )
        )
        split.addWidget(rail)
        split.setHandleWidth(14)
        split.setSizes([540, 290])
        self.dashboard_split = split
        return p

    def build_approvals(self):
        p, lay = self.page(
            "Quyền AI & ngoại lệ",
            "Các thao tác trong dự án đã cấp quyền được thực hiện tự động. Yêu cầu cũ hoặc ngoài phạm vi vẫn cần chủ máy xử lý.",
        )
        self.requests = self.tree(["Dự án / nội dung", "Loại", "Thời hạn"])
        self.requests.setColumnWidth(0, 350)
        self.requests.setColumnWidth(1, 110)
        self.requests.itemDoubleClicked.connect(lambda *_: self.review_selected())
        lay.addWidget(self.requests, 1)
        self.approval_empty = empty_state(
            "Không có yêu cầu ngoại lệ",
            "AI thao tác theo quyền từng dự án.\nCác tài liệu và checkpoint được bảo vệ.",
            "shield",
        )
        lay.addWidget(self.approval_empty, 1)
        self.approval_review = MotionButton("Xem yêu cầu", "review", "primary")
        self.approval_review.clicked.connect(self.review_selected)
        lay.addLayout(
            row(
                label("Chỉ bạn có quyền phê duyệt trên máy này.", "Fine", True),
                None,
                self.approval_review,
            )
        )
        return p

    def build_jobs(self):
        p, lay = self.page(
            "Tiến trình & kết quả",
            "Chỉ hiển thị tác vụ của Bridge. Log là dữ liệu không đáng tin và không tự cấp quyền.",
        )
        create = MotionButton("Mở desktop AI", "plus", "primary")
        create.clicked.connect(self.propose_desktop_local)
        lay.addLayout(
            row(
                label("Windows riêng · chuột/phím chỉ ở Sandbox", "Fine", True),
                None,
                create,
            )
        )
        self.jobs = self.tree(["Dự án", "Ngôn ngữ", "Trạng thái", "Job ID"])
        self.jobs.setColumnWidth(0, 190)
        self.jobs.setColumnWidth(1, 100)
        self.jobs.setColumnWidth(2, 140)
        self.jobs.setMaximumHeight(220)
        self.jobs.itemSelectionChanged.connect(self.show_job)
        lay.addWidget(self.jobs)
        self.job_tabs = QTabWidget()
        self.job_output = QPlainTextEdit()
        self.job_output.setReadOnly(True)
        self.job_output.setPlaceholderText("Chọn một tác vụ để xem đầu ra.")
        self.job_tabs.addTab(self.job_output, "Log trực tiếp")
        self.job_script = QPlainTextEdit()
        self.job_script.setReadOnly(True)
        self.job_tabs.addTab(self.job_script, "Script đã duyệt")
        lay.addWidget(self.job_tabs, 1)
        self.jobs_empty = empty_state(
            "Chưa có tác vụ",
            "Các tác vụ Python và PowerShell đã được duyệt sẽ hiển thị tiến trình và kết quả tại đây.",
            "activity",
        )
        lay.addWidget(self.jobs_empty, 1)
        self.stop_desktop = MotionButton("Dừng Sandbox", "power", "danger")
        self.stop_desktop.clicked.connect(self.stop_selected_desktop)
        self.stop_desktop.setEnabled(False)
        lay.addLayout(
            row(
                label(
                    "Chuột/phím chỉ ở guest. Đóng cửa sổ Sandbox hoặc bấm Dừng để kết thúc phiên.",
                    "Fine",
                    True,
                ),
                None,
                self.stop_desktop,
            )
        )
        return p

    def build_protection(self):
        p, lay = self.page(
            "Vùng được bảo vệ",
            "Thư mục, checkpoint, mô hình, dữ liệu và bí mật luôn nằm ngoài quyền truy cập tự động của AI.",
        )
        self.protected = self.tree(["Đường dẫn được chặn"])
        lay.addWidget(self.protected, 1)
        note = panel()
        nl = QVBoxLayout(note)
        nl.setContentsMargins(20, 18, 20, 18)
        nl.addWidget(label("Bảo vệ nhiều lớp", "H2"))
        nl.addWidget(
            label(
                "Chặn checkpoint, weights, datasets, credential và đường dẫn vượt dự án. Chặn symlink, junction và hardlink. Sửa mã nguồn đã cấp quyền sẽ tự động, kiểm tra hash và backup.",
                "Muted",
                True,
            )
        )
        lay.addWidget(note)
        protect = MotionButton("Bảo vệ thêm thư mục", "shield", "primary")
        protect.clicked.connect(self.protect)
        lay.addLayout(row(None, protect))
        return p

    def build_connection(self):
        p, lay = self.page(
            "Kết nối & cài đặt",
            "Kết nối ChatGPT Chat qua tunnel riêng. API key không xuất hiện trong giao diện hay log.",
        )
        card = panel()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(23, 23, 23, 23)
        cl.setSpacing(15)
        cl.addWidget(label("Secure MCP Tunnel", "H2"))
        self.detail_status = label("", "Muted", True)
        cl.addWidget(self.detail_status)
        self.media_auto = QCheckBox(
            "AI tự tải HLS → MP4 từ tên miền đã cho phép (không duyệt từng video)"
        )
        self.media_auto.setToolTip(
            "Chỉ HTTPS từ state/media-allowed-hosts.json, file MP4 ở Downloads, không ghi đè; giới hạn kích thước và thời lượng. Không mở host shell."
        )
        self.media_auto.toggled.connect(self.set_media_autonomy)
        cl.addWidget(self.media_auto)
        config = MotionButton("Cài đặt kết nối", "settings")
        config.clicked.connect(self.configure)
        plugin = MotionButton("Mở plugin ChatGPT", "external")
        plugin.clicked.connect(lambda: self.open_url(PLUGIN_URL))
        cl.addLayout(row(config, plugin, None))
        lay.addWidget(card)
        local = panel()
        ll = QVBoxLayout(local)
        ll.setContentsMargins(23, 23, 23, 23)
        ll.setSpacing(14)
        ll.addWidget(label("MCP cục bộ · chẩn đoán nâng cao", "H2"))
        ll.addWidget(
            label(
                "127.0.0.1:17843/mcp · bearer-protected. Không cần bật mục này khi dùng tuyến tunnel STDIO.",
                "Muted",
                True,
            )
        )
        self.local_button = MotionButton("Bật MCP cục bộ", "power")
        self.local_button.clicked.connect(self.toggle_local)
        ll.addLayout(row(self.local_button, None))
        lay.addWidget(local)
        help_text = label(
            "Sau khi Windows khởi động lại: mở Bridge và Kết nối. Trong Chat mới, chọn @Universal Desktop Bridge. Nếu cần chạy mã, gửi đề xuất từ Chat rồi duyệt script chính xác ở Phê duyệt.\n\nBản cài không gọi model API và không tự mua hay nâng cấp dịch vụ. ChatGPT và tunnel vẫn có điều kiện, hạn mức riêng.",
            "Muted",
            True,
        )
        lay.addWidget(help_text)
        lay.addStretch()
        return p

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
        lay.addLayout(
            row(
                label("Tạm dừng nhanh · Ctrl + Alt + Shift + Backspace", "Fine"),
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

    def navigate(self, index):
        self.stack.setCurrentIndex(index)
        for i, btn in enumerate(self.nav_buttons):
            btn.selected(i == index)
        self.stack.currentWidget().updateGeometry()
        self.stack.currentWidget().update()

    def guarded(self, fn):
        try:
            return fn()
        except Exception as e:
            QMessageBox.warning(self, "Bridge · không thực hiện được", str(e))

    def run_background(self, fn, done=None):
        if self.busy:
            return
        self.busy = True
        self.connect_button.setEnabled(False)
        self.local_button.setEnabled(False)
        self.configure_button.setEnabled(False)
        self.status_label.setText("●  Đang xử lý…")
        self.band.flash()
        worker = Worker(fn)
        self.workers.append(worker)

        def complete(result):
            self.busy = False
            self.configure_button.setEnabled(True)
            self.refresh()
            if done:
                done(result)
            if worker in self.workers:
                self.workers.remove(worker)

        def failed(message):
            complete(None)
            QMessageBox.warning(self, "Không kết nối được", message)

        worker.signals.done.connect(complete, Qt.ConnectionType.QueuedConnection)
        worker.signals.error.connect(failed, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(worker)

    def toggle_tunnel(self):
        if self.controller.tunnel_wanted:
            self.run_background(lambda: self.controller.stop_process("tunnel"))
        else:
            self.run_background(self.controller.start_tunnel)

    def toggle_local(self):
        self.run_background(
            lambda: (
                self.controller.stop_process("mcp")
                if self.controller.alive("mcp")
                else self.controller.start_local()
            )
        )

    def configure(self):
        if not self.busy:
            ConnectionDialog(self.controller, self).exec()

    def project_selected(self):
        self._last_project_auth_check = time.monotonic()
        selected = self.projects.selectedItems()
        self.selected_project = (
            selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None
        )
        self.sandbox_auto.blockSignals(True)
        self.sandbox_auto.setEnabled(bool(self.selected_project))
        automatic = bool(
            self.selected_project
            and self.agent.sandbox_authorization(
                self.selected_project["id"], self.selected_project["root"]
            )
        )
        self.sandbox_auto.setChecked(automatic)
        self.sandbox_auto.blockSignals(False)
        self.source_auto.blockSignals(True)
        source_granted = bool(
            self.selected_project
            and self.agent.source_autonomy_authorization(
                self.selected_project["id"], self.selected_project["root"]
            )
        )
        self.source_auto.setEnabled(
            bool(self.selected_project and self.selected_project["writable"])
        )
        self.source_auto.setChecked(source_granted)
        self.source_auto.blockSignals(False)
        if self.selected_project:
            self.footer_project.setText("Dự án: " + self.selected_project["id"])
            self.project_path.setText(self.selected_project["root"])
            self.project_path.setCursorPosition(0)
            self.project_path.setToolTip(self.selected_project["root"])
            self.project_access.setText(
                (
                    "AI tự sửa mã nguồn hợp lệ, có backup."
                    if source_granted
                    else "Được ghi: bật AI tự sửa để không phải duyệt."
                    if self.selected_project["writable"]
                    else "File gốc: chỉ đọc."
                )
                + (
                    " Sandbox: tự động."
                    if automatic
                    else " Sandbox: chưa cấp quyền tự động."
                )
            )
        else:
            self.footer_project.setText("Chưa chọn dự án")
            self.project_path.clear()
            self.project_path.setToolTip("")
            self.project_access.setText("Thêm thư mục nguồn để bắt đầu.")

    def set_sandbox_autonomy(self, enabled):
        if self.selected_project:
            self.guarded(
                lambda: self.agent.set_sandbox_autonomy_local(
                    self.selected_project["id"], enabled
                )
            )
        self.project_selected()
        self.refresh()

    def set_source_autonomy(self, enabled):
        if not self.selected_project:
            return
        if enabled:
            ok = QMessageBox.question(
                self,
                "Bật AI tự sửa mã nguồn",
                "Cấp quyền ghi tự động cho AI trong dự án "
                + self.selected_project["id"]
                + "?\n"
                "Không cần phê duyệt từng file. File quan trọng vẫn bị chặn; mỗi lần sửa lưu backup.\n"
                "Chọn No để giữ quyền hiện tại.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                self.project_selected()
                return
        self.guarded(
            lambda: self.agent.set_source_autonomy_local(
                self.selected_project["id"], enabled
            )
        )
        self.project_selected()
        self.refresh()

    def set_media_autonomy(self, enabled):
        if enabled:
            ok = QMessageBox.question(
                self,
                "Cho phép tải video tự động",
                "Cấp quyền tải HLS HTTPS từ các tên miền được chủ máy cho phép, "
                "chỉ lưu MP4 trong Downloads, không hỏi từng video?\n"
                "Có thể tốn băng thông/dung lượng. Không cho phép chạy lệnh Windows.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                self.media_auto.blockSignals(True)
                self.media_auto.setChecked(False)
                self.media_auto.blockSignals(False)
                return
        self.guarded(lambda: self.agent.set_media_autonomy_local(enabled))
        self.refresh()

    def open_source(self):
        if self.selected_project:
            self.guarded(
                lambda: SourceDialog(
                    self.agent, self.selected_project["id"], self
                ).exec()
            )

    def add_project(self):
        folder = QFileDialog.getExistingDirectory(self, "Chọn thư mục CHỈ mã nguồn")
        if not folder:
            return
        name, ok = QInputDialog.getText(
            self, "Tên dự án", "ID ngắn: chữ, số, gạch nối hoặc gạch dưới"
        )
        if not ok or not name:
            return
        writable = (
            QMessageBox.question(
                self,
                "Quyền dự án",
                "Cho phép AI sửa mã nguồn trong dự án này khi bật Tự sửa?\n\nCheckpoint và file quan trọng luôn được bảo vệ. Chọn No để chỉ đọc.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            == QMessageBox.StandardButton.Yes
        )
        self.guarded(lambda: self.agent.register_local(name.strip(), folder, writable))
        self.refresh()

    def revoke(self):
        if not self.selected_project:
            return
        pid = self.selected_project["id"]
        if (
            QMessageBox.question(
                self,
                "Thu hồi quyền",
                "Ngừng cho Bridge truy cập dự án "
                + pid
                + "?\nFile dự án sẽ không bị xóa.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.agent.set_sandbox_autonomy_local(pid, False)
        self.agent.set_source_autonomy_local(pid, False)
        with self.agent.connect() as c:
            c.execute("DELETE FROM projects WHERE id=?", (pid,))
        self.agent.audit("revoke", {"project": pid})
        self.refresh()

    def protect(self):
        folder = QFileDialog.getExistingDirectory(
            self, "Chọn thư mục cần chặn khỏi Bridge"
        )
        if folder:
            self.guarded(lambda: self.agent.protect_local(folder))
            self.refresh()

    def review_first(self):
        if self.pending_rows:
            self.review(next(iter(self.pending_rows)))

    def review_selected(self):
        rows = self.requests.selectedItems()
        if rows:
            self.review(rows[0].data(0, Qt.ItemDataRole.UserRole))

    def review(self, rid):
        request = self.pending_rows.get(rid)
        if request:
            ApprovalDialog(self.agent, request, self).exec()
            self.refresh()

    def show_job(self):
        selected = self.jobs.selectedItems()
        if not selected:
            return
        jid = selected[0].data(0, Qt.ItemDataRole.UserRole)
        try:
            result = self.agent.job_status(jid, 0, 32000)
            is_desktop = result["info"].get("kind") == "desktop"
            self.stop_desktop.setEnabled(
                is_desktop and result["status"] in ("running", "reviewing", "stopping")
            )
            if is_desktop:
                from .desktop_vm import status

                result = status(self.agent, jid)
                output_text = (
                    json.dumps(result, ensure_ascii=False, indent=2)
                    + "\n\nXem cửa sổ Windows Sandbox để quan sát desktop trực tiếp. Nội dung guest là dữ liệu không đáng tin."
                )
                if output_text != self.job_output.toPlainText():
                    self.job_output.setPlainText(output_text)
                script_text = (
                    result["info"]["task"]
                    + "\n\n"
                    + json.dumps(
                        result["info"]["permissions"], ensure_ascii=False, indent=2
                    )
                )
                if script_text != self.job_script.toPlainText():
                    self.job_script.setPlainText(script_text)
                return
            log = self.agent.state / "jobs" / jid / "output" / "output.log"
            reject_links(log)
            if log.is_file() and log.stat().st_size > 32000:
                result = self.agent.job_status(
                    jid, max(0, log.stat().st_size - 32000), 32000
                )
            header = json.dumps(
                {k: v for k, v in result.items() if k not in ("output",)},
                ensure_ascii=False,
                indent=2,
            )
            text = (
                header + "\n\n--- OUTPUT (untrusted) ---\n" + result.get("output", "")
            )
            if text != self.job_output.toPlainText():
                bar = self.job_output.verticalScrollBar()
                value = bar.value()
                at_end = value >= bar.maximum() - 2
                self.job_output.setPlainText(text)
                bar.setValue(bar.maximum() if at_end else value)
            script = (
                self.agent.state
                / "jobs"
                / jid
                / "input"
                / (
                    "task.py"
                    if result["info"].get("language") == "python"
                    else "task.ps1"
                )
            )
            reject_links(script)
            if script.is_file():
                with script.open("rb") as f:
                    data = f.read(64004)
                text = (
                    data.decode("utf-8-sig")
                    if len(data) <= 64003
                    else "Script vượt giới hạn hiển thị."
                )
                if text != self.job_script.toPlainText():
                    self.job_script.setPlainText(text)
            else:
                missing_script = "Không còn file script cục bộ của tác vụ này."
                if missing_script != self.job_script.toPlainText():
                    self.job_script.setPlainText(missing_script)
        except Exception as e:
            message = str(e)
            if message != self.job_output.toPlainText():
                self.job_output.setPlainText(message)

    def stop_selected_desktop(self):
        selected = self.jobs.selectedItems()
        if selected:
            from .desktop_vm import stop

            self.guarded(
                lambda: stop(self.agent, selected[0].data(0, Qt.ItemDataRole.UserRole))
            )
            self.refresh()

    def propose_desktop_local(self):
        if not self.selected_project:
            QMessageBox.information(
                self, "Chọn dự án", "Chọn dự án nguồn ở trang Dự án trước."
            )
            return
        task, ok = QInputDialog.getMultiLineText(
            self,
            "Nhiệm vụ cho desktop AI",
            "AI được làm gì trong Sandbox? Phiên 10 phút, mạng/clipboard tắt.",
            "Mở UDB Sandbox Pad, gõ UDB_SANDBOX_INPUT_OK, bấm Confirm và chụp màn hình kiểm tra. Chỉ thao tác trong guest Windows Sandbox.",
        )
        if ok and task.strip():
            response = self.guarded(
                lambda: self.agent.propose_desktop(
                    self.selected_project["id"], task.strip(), 600
                )
            )
            self.refresh()
            if response:
                if response["status"] == "queued_sandbox":
                    self.navigate(2)
                else:
                    self.review(response["request_id"])

    def open_url(self, url):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl(url))

    def showEvent(self, event):
        super().showEvent(event)
        if os.name == "nt":
            try:
                import ctypes

                value = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), 20, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                pass  # DWM styling is cosmetic and may be unsupported.

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
            return
        if not self._closing:
            self._closing = True
            self.timer.stop()
            self.controller.close()
        event.accept()


def make_app():
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Universal Desktop Bridge")
    app.setOrganizationName("UniversalDesktopBridge")
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(STYLE)
    app.setProperty("motion", True)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#28483f"))
    app.setPalette(palette)
    return app


def run(home: Path, start_tunnel=False, screenshot=None):
    if os.name == "nt":
        import ctypes

        ctypes.windll.kernel32.FreeConsole()
    app = make_app()
    controller = DesktopController(home)
    settings = controller.settings()
    app.setProperty("motion", settings.get("motion", True))
    lock = QLockFile(str(controller.agent.state / "manager.lock"))
    lock.setStaleLockTime(30000)
    if not lock.tryLock(50):
        QMessageBox.information(
            None,
            "Bridge đang mở",
            "Ứng dụng đã được mở. Kiểm tra cửa sổ hoặc thanh tác vụ Windows.",
        )
        return
    controller.start_host()
    window = MainWindow(home, controller)
    window.show()
    if start_tunnel or settings.get("autoconnect", False):
        QTimer.singleShot(350, window.toggle_tunnel)
    if screenshot:
        dest = Path(screenshot)

        def capture():
            if not window.grab().save(str(dest)):
                raise OSError("GUI screenshot failed")
            app.quit()

        QTimer.singleShot(7000 if start_tunnel else 550, capture)
    code = app.exec()
    controller.close()
    lock.unlock()
    return code


def self_test(preview_dir=None):
    """Run the disposable Qt GUI self-test while preserving the public API."""
    from .manager_selftest import self_test as run_self_test

    return run_self_test(preview_dir)
