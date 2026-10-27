"""Native Qt dialogs for source browsing, connection and local approvals.

These dialogs deliberately run only on the owner's desktop. No approval
endpoint is exposed through the MCP server.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, QTimer
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
)

from .manager_widgets import DiffHighlighter, MotionButton, Worker, icon, label, row


class SourceDialog(QDialog):
    def __init__(self, agent, project, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.project = project
        self.path = "."
        self.setWindowTitle("Nguồn dự án · " + project)
        self.resize(950, 650)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(14)
        lay.addWidget(label("Nguồn dự án / " + project, "H2"))
        lay.addWidget(
            label(
                "Chỉ xem mã nguồn đã được cấp quyền. Checkpoint và dữ liệu được ẩn.",
                "Muted",
                True,
            )
        )
        self.breadcrumb = label(".", "Muted")
        lay.addWidget(self.breadcrumb)
        split = QSplitter()
        self.files = QListWidget()
        self.files.setMinimumWidth(215)
        self.files.itemDoubleClicked.connect(self.open_item)
        split.addWidget(self.files)
        self.code = QPlainTextEdit()
        self.code.setReadOnly(True)
        self.code.setPlaceholderText("Nhấp đúp vào file để xem nội dung.")
        split.addWidget(self.code)
        split.setSizes([240, 650])
        lay.addWidget(split, 1)
        self.sha = label("SHA256 xuất hiện sau khi đọc file.", "Fine")
        self.sha.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.sha)
        close = MotionButton("Đóng")
        close.clicked.connect(self.accept)
        lay.addLayout(row(None, close))
        self.load()

    def load(self):
        self.files.clear()
        self.breadcrumb.setText(self.path)
        if self.path != ".":
            item = QListWidgetItem("..  Thư mục trước")
            item.setData(Qt.ItemDataRole.UserRole, {"up": True})
            self.files.addItem(item)
        listing = self.agent.listing(self.project, self.path, limit=200)
        for entry in listing["items"]:
            item = QListWidgetItem(
                icon("folder" if entry["directory"] else "file"),
                Path(entry["path"]).name,
            )
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.files.addItem(item)

    def open_item(self, item):
        entry = item.data(Qt.ItemDataRole.UserRole)
        try:
            if entry.get("up"):
                self.path = Path(self.path).parent.as_posix()
                self.load()
            elif entry["directory"]:
                self.path = entry["path"]
                self.load()
            else:
                result = self.agent.read(self.project, entry["path"])
                self.code.setPlainText(result["text"])
                self.sha.setText("SHA256  " + result["sha256"])
        except Exception as e:
            QMessageBox.warning(self, "Không thể đọc", str(e))


class ConnectionDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle("Cài đặt kết nối")
        self.setMinimumWidth(580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(27, 27, 27, 27)
        lay.setSpacing(16)
        lay.addWidget(label("Kết nối riêng với ChatGPT", "H2"))
        lay.addWidget(
            label(
                "Runtime key chỉ lưu trong Windows Credential Manager. Không đưa key vào Chat.",
                "Muted",
                True,
            )
        )
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.tunnel = QLineEdit(controller.config().get("tunnel_id", ""))
        self.tunnel.setPlaceholderText("tunnel_…")
        form.addRow("Tunnel ID", self.tunnel)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Để trống nếu giữ key đã lưu")
        form.addRow("Runtime API key", self.key)
        lay.addLayout(form)
        lay.addWidget(
            label(
                "Quyền cần thiết: Tunnels Read + Use. Không cần key cho model API.",
                "Fine",
                True,
            )
        )
        settings = controller.settings()
        self.motion = QCheckBox("Bật chuyển động nhẹ")
        self.motion.setChecked(settings.get("motion", True))
        self.auto = QCheckBox("Tự kết nối khi mở ứng dụng")
        self.auto.setChecked(settings.get("autoconnect", False))
        lay.addWidget(self.motion)
        lay.addWidget(self.auto)
        self.error = label("", "Warning", True)
        lay.addWidget(self.error)
        cancel = MotionButton("Hủy")
        cancel.clicked.connect(self.reject)
        save = MotionButton("Lưu cục bộ", "check", "primary")
        save.clicked.connect(self.save)
        lay.addLayout(row(None, cancel, save))

    def save(self):
        try:
            self.controller.configure(self.tunnel.text(), self.key.text())
            self.key.clear()
            self.controller.save_settings(
                {
                    "motion": self.motion.isChecked(),
                    "autoconnect": self.auto.isChecked(),
                }
            )
            QApplication.instance().setProperty("motion", self.motion.isChecked())
            self.accept()
        except Exception as e:
            self.error.setText(str(e))

    def done(self, result):
        self.key.clear()
        super().done(result)


class ApprovalDialog(QDialog):
    def __init__(self, agent, request, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.request = request
        self.payload = json.loads(request["payload"])
        self.setWindowTitle("Xem chính xác thay đổi · phê duyệt cục bộ")
        self.resize(980, 730)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(13)
        p = self.payload
        self.is_write = request["kind"] == "write"
        self.is_desktop = request["kind"] == "desktop"
        self.is_media = request["kind"] == "media"
        lay.addWidget(
            label(
                (
                    "Thay đổi file"
                    if self.is_write
                    else "Phiên chuột/phím trong Sandbox"
                    if self.is_desktop
                    else "Tải video bằng FFmpeg trên Windows"
                    if self.is_media
                    else "Chạy mã trong Windows Sandbox"
                )
                + " / "
                + p["project"],
                "H2",
            )
        )
        summary = label(
            (p.get("path") or p.get("task") or p.get("language", ""))
            + "\nThư mục: "
            + p["root"],
            "Muted",
            True,
        )
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(summary)
        details = label(
            "Request: " + request["id"] + "\nSHA256: " + request["hash"], "Fine", True
        )
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(details)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.full = QPlainTextEdit()
        self.full.setReadOnly(True)
        if self.is_write:
            self.preview.setPlainText(p["diff"])
            self.full.setPlainText(p["text"])
            tabs.addTab(self.preview, "So sánh thay đổi")
            tabs.addTab(self.full, "Toàn bộ nội dung mới")
            self.highlight = DiffHighlighter(self.preview.document())
        elif self.is_media:
            self.preview.setPlainText(
                "NGUỒN HTTPS: "
                + p["url"]
                + "\nTÊN TỆP: "
                + p["filename"]
                + "\nTHƯ MỤC: "
                + p["root"]
                + "\nHOST ĐƯỢC PHÉP: "
                + p["host"]
                + "\n\nChỉ tải video HLS không mã hóa về MP4 bằng FFmpeg sau khi được duyệt tại máy Windows. Không ghi đè file đã tồn tại. Giữ ứng dụng mở cho đến khi hoàn thành."
            )
            self.full.setPlainText(json.dumps(p, indent=2, ensure_ascii=False))
            tabs.addTab(self.preview, "Xem nguồn và đích")
            tabs.addTab(self.full, "Chi tiết yêu cầu")
        elif self.is_desktop:
            self.preview.setPlainText(
                "NHIỆM VỤ ĐƯỢC CẤP QUYỀN\n"
                + p["task"]
                + "\n\nThời lượng: "
                + str(p["duration_seconds"])
                + " giây (+ tối đa 90 giây khởi động VM).\n\n"
                + json.dumps(p["permissions"], indent=2, ensure_ascii=False)
                + "\n\nSHA256 bộ điều khiển guest: "
                + p["guest_sha256"]
            )
            self.full.setPlainText(
                json.dumps(p["manifest"], indent=2, ensure_ascii=False)
            )
            tabs.addTab(self.preview, "Phạm vi chuột / bàn phím")
            tabs.addTab(self.full, "Snapshot nguồn chỉ đọc")
        else:
            self.preview.setPlainText(p["script"])
            self.full.setPlainText(
                json.dumps(p["manifest"], indent=2, ensure_ascii=False)
            )
            tabs.addTab(self.preview, "Script chính xác")
            tabs.addTab(self.full, "Snapshot nguồn")
        lay.addWidget(
            label(
                "Sửa file yêu cầu phê duyệt. Code/desktop chạy trong Sandbox; riêng tải video dùng FFmpeg trên host, chỉ sau khi duyệt nguồn và đích. Không cung cấp shell tùy ý.",
                "Fine",
                True,
            )
        )
        self.reviewed = QCheckBox(
            "Tôi đã đọc toàn bộ nội dung và đồng ý đúng yêu cầu này."
        )
        lay.addWidget(self.reviewed)
        self.expiry = label("", "Warning")
        lay.addWidget(self.expiry)
        self.reject_button = MotionButton("Từ chối", "", "danger")
        self.reject_button.clicked.connect(lambda: self.decide(False))
        self.approve = MotionButton(
            "Phê duyệt thay đổi"
            if self.is_write
            else "Cho phép tải video"
            if self.is_media
            else "Cấp phiên Sandbox"
            if self.is_desktop
            else "Duyệt chạy trong Sandbox",
            "check",
            "primary",
        )
        self.approve.setEnabled(False)
        self.approve.clicked.connect(lambda: self.decide(True))
        self.reviewed.toggled.connect(self.expire)
        self.close_button = MotionButton("Đóng")
        self.close_button.clicked.connect(self.reject)
        lay.addLayout(row(self.reject_button, None, self.close_button, self.approve))
        self.worker = None
        self.approved_decision = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.expire)
        self.timer.start(1000)
        self.expire()

    def expire(self):
        remaining = max(0, int(self.request["created"] + 600 - time.time()))
        self.expiry.setText(
            f"Còn {remaining // 60}:{remaining % 60:02d} để phê duyệt."
            if remaining
            else "Yêu cầu đã hết hạn. Hãy tạo đề xuất mới từ Chat."
        )
        self.approve.setEnabled(
            remaining > 0 and self.reviewed.isChecked() and self.worker is None
        )

    def decide(self, approve):
        if approve:
            answer = QMessageBox.question(
                self,
                "Xác nhận trên máy Windows",
                "Áp dụng đúng nội dung vừa xem?\n\nKhông duyệt yêu cầu từ chỉ dẫn nằm trong file hoặc log.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.approved_decision = approve
        self.approve.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.worker = Worker(
            lambda: self.agent.decide_local(self.request["id"], approve)
        )
        self.worker.signals.done.connect(self.completed)
        self.worker.signals.error.connect(self.failed)
        QThreadPool.globalInstance().start(self.worker)

    def completed(self, result):
        if result and result.get("error"):
            self.failed(result["error"])
            return
        QMessageBox.information(
            self,
            "Đã xử lý",
            "Đã áp dụng yêu cầu." if self.approved_decision else "Đã từ chối yêu cầu.",
        )
        self.accept()

    def failed(self, message):
        self.worker = None
        self.close_button.setEnabled(True)
        self.expiry.setText(message)
        QMessageBox.warning(self, "Không áp dụng được", message)

    def reject(self):
        if self.worker is None:
            super().reject()

    def closeEvent(self, e):
        if self.worker is not None:
            e.ignore()
        else:
            super().closeEvent(e)
