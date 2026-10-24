"""Obsidian Studio: native Qt UI. Host approvals and Sandbox autonomy grants stay local."""

from __future__ import annotations
import json
import os
import sys
import time
from pathlib import Path
from PySide6.QtCore import (
    Qt,
    QSize,
    QRectF,
    QPointF,
    QTimer,
    QPropertyAnimation,
    Property,
    QEasingCurve,
    QObject,
    Signal,
    QRunnable,
    QThreadPool,
    QLockFile,
)
from PySide6.QtGui import (
    QColor,
    QPainter,
    QPen,
    QPainterPath,
    QPixmap,
    QIcon,
    QFont,
    QLinearGradient,
    QSyntaxHighlighter,
    QTextCharFormat,
    QPalette,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QFrame,
    QLabel,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QStackedWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QHeaderView,
    QAbstractItemView,
    QPlainTextEdit,
    QSplitter,
    QDialog,
    QLineEdit,
    QFormLayout,
    QCheckBox,
    QFileDialog,
    QInputDialog,
    QMessageBox,
    QTabWidget,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QSizePolicy,
    QStyleFactory,
)
from .desktop import DesktopController
from .core import PolicyError, reject_links
from . import __version__

BG = '#101713'
PANEL = '#19231d'
RAISED = '#25362b'
LINE = '#405448'
TEXT = '#f2f7f3'
MUTED = '#c0cec4'
ACCENT = '#b9e3c6'
WARNING = '#f0cf9c'
DANGER = '#f1b8ac'
PLUGIN_URL = 'https://chatgpt.com/plugins/plugin_asdk_app_6ad78a6c547c81919d4efa885507ead2'

STYLE = f'''
QWidget {{ color:{TEXT}; font-family:"Segoe UI"; font-size:14px; }}
QMainWindow, QDialog {{ background:{BG}; }}
QWidget#Root, QWidget#Page, QWidget#PageHeader, QStackedWidget {{ background:{BG}; }}
QFrame#Sidebar {{ background:{PANEL}; border-right:1px solid {LINE}; }}
QFrame#Panel {{ background:{PANEL}; border:1px solid {LINE}; border-radius:12px; }}
QFrame#Footer {{ border-top:1px solid {LINE}; background:{BG}; }}
QLabel {{ background:transparent; border:0; }}
QLabel#H1 {{ font-size:28px; font-weight:600; }}
QLabel#H2 {{ font-size:18px; font-weight:600; }}
QLabel#EmptyTitle {{ font-size:21px; font-weight:600; }}
QLabel#EmptyIcon {{ color:{ACCENT}; font-size:32px; }}
QLabel#Subtitle {{ color:{MUTED}; font-size:14px; }}
QLabel#Brand {{ font-size:26px; font-weight:600; letter-spacing:-1px; }}
QLabel#Eyebrow {{ color:{MUTED}; font-size:12px; letter-spacing:1.2px; }}
QLabel#Muted {{ color:{MUTED}; font-size:14px; }}
QLabel#Fine {{ color:{MUTED}; font-size:13px; }}
QLabel#Warning {{ color:{WARNING}; font-size:12px; }}
QLabel#Badge {{ background:{RAISED}; color:{ACCENT}; border-radius:9px; padding:3px 8px; }}
QLineEdit, QPlainTextEdit, QListWidget, QTreeWidget {{ background:{BG}; color:{TEXT}; border:1px solid {LINE}; border-radius:8px; selection-background-color:#345040; selection-color:{TEXT}; }}
QLineEdit {{ padding:10px 12px; }}
QLineEdit#ProjectPath {{ background:transparent; border:0; padding:0; color:{MUTED}; font-size:12px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTreeWidget:focus, QListWidget:focus {{ border-color:#7b9f87; }}
QPlainTextEdit {{ padding:12px; font-family:"Cascadia Code",Consolas,monospace; font-size:13px; }}
QTreeWidget {{ border:0; outline:0; background:transparent; alternate-background-color:{PANEL}; }}
QTreeWidget::item {{ padding:13px 7px; border-bottom:1px solid {LINE}; }}
QTreeWidget::item:selected {{ background:#283c30; color:{TEXT}; }}
QTreeWidget::item:hover {{ background:#202f26; }}
QHeaderView::section {{ background:{BG}; color:{MUTED}; border:0; border-bottom:1px solid {LINE}; padding:10px 7px; font-size:12px; }}
QListWidget::item {{ padding:10px; }}
QListWidget::item:selected {{ background:#283c30; }}
QScrollArea {{ background:transparent; border:0; }}
QScrollBar:vertical {{ background:{BG}; width:10px; margin:0; }}
QScrollBar::handle:vertical {{ background:#43594a; min-height:28px; border-radius:5px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar:horizontal {{ background:{BG}; height:10px; }}
QScrollBar::handle:horizontal {{ background:#43594a; min-width:28px; border-radius:5px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
QTabWidget::pane {{ border:1px solid {LINE}; border-radius:7px; }}
QTabBar::tab {{ background:{PANEL}; color:{MUTED}; padding:10px 16px; border-bottom:2px solid transparent; }}
QTabBar::tab:selected {{ color:{TEXT}; border-bottom:2px solid {ACCENT}; }}
QCheckBox {{ spacing:10px; padding:7px 0; }}
QCheckBox::indicator {{ width:17px; height:17px; background:{BG}; border:1px solid #718c7a; border-radius:4px; }}
QCheckBox::indicator:checked {{ background:{ACCENT}; border:4px solid #375a43; }}
QToolTip {{ background:{RAISED}; color:{TEXT}; border:1px solid {LINE}; padding:7px; }}
QMessageBox {{ background:{BG}; }}
QMessageBox QPushButton, QInputDialog QPushButton {{ background:{RAISED}; border:1px solid {LINE}; border-radius:6px; padding:8px 18px; min-width:75px; }}
QMessageBox QPushButton:hover, QInputDialog QPushButton:hover {{ background:#314438; }}
QSplitter::handle {{ background:transparent; width:1px; }}
'''


def label(text: str = '', name: str = '', wrap: bool = False) -> QLabel:
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    w.setMinimumHeight(22)
    return w


def row(*widgets: QWidget | None) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    for w in widgets:
        if w is None:
            layout.addStretch()
        else:
            layout.addWidget(w)
    return layout


def panel() -> QFrame:
    w = QFrame()
    w.setObjectName('Panel')
    return w


def empty_state(title: str, description: str, glyph: str = 'check') -> QFrame:
    card = panel()
    lay = QVBoxLayout(card)
    lay.setContentsMargins(28, 24, 28, 24)
    lay.setSpacing(12)
    lay.addStretch()
    mark = QLabel()
    mark.setPixmap(icon(glyph, ACCENT, 40).pixmap(40, 40))
    mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
    mark.setMinimumHeight(48)
    lay.addWidget(mark)
    heading = label(title, 'EmptyTitle', True)
    heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(heading)
    body = label(description, 'Muted', True)
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.setMinimumHeight(64)
    lay.addWidget(body)
    lay.addStretch()
    return card


def icon(name: str, color: str = TEXT, size: int = 20) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size * 2 / 24, size * 2 / 24)
    p.setPen(QPen(QColor(color), 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
    if name in ('folder', 'projects'):
        path = QPainterPath()
        path.moveTo(3, 6)
        path.lineTo(9, 6)
        path.lineTo(11, 9)
        path.lineTo(21, 9)
        path.lineTo(21, 20)
        path.lineTo(3, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif name == 'shield':
        path = QPainterPath()
        path.moveTo(12, 3)
        path.lineTo(20, 6)
        path.lineTo(19, 14)
        path.quadTo(17, 19, 12, 22)
        path.quadTo(7, 19, 5, 14)
        path.lineTo(4, 6)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(8, 12, 11, 15)
        p.drawLine(11, 15, 16, 10)
    elif name == 'power':
        p.drawArc(QRectF(4, 4, 16, 16), 40 * 16, 280 * 16)
        p.drawLine(12, 2, 12, 11)
    elif name == 'plus':
        p.drawLine(12, 5, 12, 19)
        p.drawLine(5, 12, 19, 12)
    elif name == 'arrow':
        p.drawLine(5, 12, 19, 12)
        p.drawLine(14, 7, 19, 12)
        p.drawLine(14, 17, 19, 12)
    elif name == 'activity':
        path = QPainterPath()
        path.moveTo(2, 12)
        path.lineTo(7, 12)
        path.lineTo(10, 5)
        path.lineTo(14, 19)
        path.lineTo(17, 12)
        path.lineTo(22, 12)
        p.drawPath(path)
    elif name == 'review':
        p.drawEllipse(QRectF(4, 3, 5, 5))
        p.drawLine(7, 8, 7, 20)
        p.drawLine(7, 12, 17, 12)
        p.drawLine(17, 12, 17, 20)
        p.drawEllipse(QRectF(14.5, 3, 5, 5))
        p.drawLine(17, 8, 17, 12)
    elif name == 'settings':
        for y in (6, 12, 18):
            p.drawLine(3, y, 21, y)
        for x, y in ((8, 6), (16, 12), (10, 18)):
            p.fillRect(QRectF(x - 2, y - 2, 4, 4), QColor(PANEL))
            p.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
    elif name == 'file':
        path = QPainterPath()
        path.moveTo(6, 3)
        path.lineTo(14, 3)
        path.lineTo(19, 8)
        path.lineTo(19, 21)
        path.lineTo(6, 21)
        path.closeSubpath()
        p.drawPath(path)
        p.drawLine(14, 3, 14, 8)
        p.drawLine(14, 8, 19, 8)
        p.drawLine(9, 13, 16, 13)
        p.drawLine(9, 17, 15, 17)
    elif name == 'external':
        p.drawLine(13, 4, 20, 4)
        p.drawLine(20, 4, 20, 11)
        p.drawLine(20, 4, 10, 14)
        p.drawLine(10, 5, 4, 5)
        p.drawLine(4, 5, 4, 20)
        p.drawLine(4, 20, 19, 20)
        p.drawLine(19, 20, 19, 14)
    elif name == 'check':
        p.drawLine(5, 12, 10, 17)
        p.drawLine(10, 17, 20, 6)
    else:
        p.drawRoundedRect(QRectF(4, 5, 16, 14), 3, 3)
        p.drawLine(8, 10, 16, 10)
        p.drawLine(8, 14, 14, 14)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


class MotionButton(QPushButton):
    def __init__(self, text='', glyph='', kind='quiet', parent=None):
        super().__init__(text, parent)
        self.glyph = glyph
        self.kind = kind
        self._hover = 0.0
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(40)
        self.setAccessibleName(text)
        self.anim = QPropertyAnimation(self, b'hover', self)
        self.anim.setDuration(160)
        self.anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def setText(self, text):
        super().setText(text)
        self.setAccessibleName(text)
        self.updateGeometry()
        self.update()

    def get_hover(self):
        return self._hover

    def set_hover(self, v):
        self._hover = v
        self.update()

    hover = Property(float, get_hover, set_hover)

    def selected(self, v):
        self._selected = v
        self.update()

    def enterEvent(self, e):
        self.animate(1.0)
        super().enterEvent(e)

    def leaveEvent(self, e):
        self.animate(0.0)
        super().leaveEvent(e)

    def animate(self, v):
        if not QApplication.instance().property('motion'):
            self.set_hover(v)
            return
        self.anim.stop()
        self.anim.setStartValue(self._hover)
        self.anim.setEndValue(v)
        self.anim.start()

    def sizeHint(self):
        return QSize(max(88, self.fontMetrics().horizontalAdvance(self.text()) + 44 + (24 if self.glyph else 0)), 42)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QColor(ACCENT if self.kind == 'primary' else ('#283c30' if self._selected else PANEL))
        over = QColor('#ccecD4' if self.kind == 'primary' else '#2c4033')
        bg = QColor(*[round(base.getRgb()[i] * (1 - self._hover) + over.getRgb()[i] * self._hover) for i in range(3)])
        fg = QColor('#173322' if self.kind == 'primary' else (DANGER if self.kind == 'danger' else TEXT))
        if not self.isEnabled():
            bg = QColor('#263129')
            fg = QColor('#85968b')
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(bg)
        p.setPen(QPen(QColor(ACCENT if self.hasFocus() else (LINE if self.kind != 'primary' else ACCENT)), 1))
        if self.kind == 'nav' and not self.hasFocus():
            p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 8, 8)
        offset = 1 if self.isDown() else 0
        width = self.fontMetrics().horizontalAdvance(self.text()) + (25 if self.glyph else 0)
        x = 16 if self.kind == 'nav' else (self.width() - width) / 2
        if self.glyph:
            icon(self.glyph, fg.name()).paint(p, int(x), int((self.height() - 18) / 2) + offset, 18, 18)
            x += 26
        p.setPen(fg)
        p.setFont(self.font())
        p.drawText(QRectF(x, offset, self.width() - x - 9, self.height()), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()


class StatusBand(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName('Panel')
        self._progress = 1.0
        self.anim = QPropertyAnimation(self, b'progress', self)
        self.anim.setDuration(1050)
        self.anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def get_progress(self):
        return self._progress

    def set_progress(self, v):
        self._progress = v
        self.update()

    progress = Property(float, get_progress, set_progress)

    def flash(self):
        if not QApplication.instance().property('motion'):
            return
        self.anim.stop()
        self.anim.setStartValue(0.0)
        self.anim.setEndValue(1.0)
        self.anim.start()

    def paintEvent(self, e):
        super().paintEvent(e)
        if 0 < self._progress < 1:
            p = QPainter(self)
            x = (self.width() + 220) * self._progress - 220
            g = QLinearGradient(x, 0, x + 220, 0)
            g.setColorAt(0, QColor('#00b9e3c6'))
            g.setColorAt(0.5, QColor(ACCENT))
            g.setColorAt(1, QColor('#00b9e3c6'))
            p.fillRect(QRectF(x, self.height() - 2, 220, 2), g)
            p.end()


class DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        if text.startswith('+') and not text.startswith('+++'):
            color = ACCENT
            background = '#22392a'
        elif text.startswith('-') and not text.startswith('---'):
            color = DANGER
            background = '#392924'
        elif text.startswith('@@'):
            color = WARNING
            background = None
        else:
            return
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        if background:
            fmt.setBackground(QColor(background))
        self.setFormat(0, len(text), fmt)


class WorkerSignals(QObject):
    done = Signal(object)
    error = Signal(str)


class Worker(QRunnable):
    def __init__(self, fn):
        super().__init__()
        self.fn = fn
        self.signals = WorkerSignals()

    def run(self):
        try:
            self.signals.done.emit(self.fn())
        except Exception as e:
            self.signals.error.emit(str(e))


class SourceDialog(QDialog):
    def __init__(self, agent, project, parent=None):
        super().__init__(parent)
        self.agent = agent
        self.project = project
        self.path = '.'
        self.setWindowTitle('Nguồn dự án · ' + project)
        self.resize(950, 650)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(14)
        lay.addWidget(label('Nguồn dự án / ' + project, 'H2'))
        lay.addWidget(label('Chỉ xem mã nguồn đã được cấp quyền. Checkpoint và dữ liệu được ẩn.', 'Muted', True))
        self.breadcrumb = label('.', 'Muted')
        lay.addWidget(self.breadcrumb)
        split = QSplitter()
        self.files = QListWidget()
        self.files.setMinimumWidth(215)
        self.files.itemDoubleClicked.connect(self.open_item)
        split.addWidget(self.files)
        self.code = QPlainTextEdit()
        self.code.setReadOnly(True)
        self.code.setPlaceholderText('Nhấp đúp vào file để xem nội dung.')
        split.addWidget(self.code)
        split.setSizes([240, 650])
        lay.addWidget(split, 1)
        self.sha = label('SHA256 xuất hiện sau khi đọc file.', 'Fine')
        self.sha.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self.sha)
        close = MotionButton('Đóng')
        close.clicked.connect(self.accept)
        lay.addLayout(row(None, close))
        self.load()

    def load(self):
        self.files.clear()
        self.breadcrumb.setText(self.path)
        if self.path != '.':
            item = QListWidgetItem('..  Thư mục trước')
            item.setData(Qt.ItemDataRole.UserRole, {'up': True})
            self.files.addItem(item)
        listing = self.agent.listing(self.project, self.path, limit=200)
        for entry in listing['items']:
            item = QListWidgetItem(icon('folder' if entry['directory'] else 'file'), Path(entry['path']).name)
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self.files.addItem(item)

    def open_item(self, item):
        entry = item.data(Qt.ItemDataRole.UserRole)
        try:
            if entry.get('up'):
                self.path = Path(self.path).parent.as_posix()
                self.load()
            elif entry['directory']:
                self.path = entry['path']
                self.load()
            else:
                result = self.agent.read(self.project, entry['path'])
                self.code.setPlainText(result['text'])
                self.sha.setText('SHA256  ' + result['sha256'])
        except Exception as e:
            QMessageBox.warning(self, 'Không thể đọc', str(e))


class ConnectionDialog(QDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.setWindowTitle('Cài đặt kết nối')
        self.setMinimumWidth(580)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(27, 27, 27, 27)
        lay.setSpacing(16)
        lay.addWidget(label('Kết nối riêng với ChatGPT', 'H2'))
        lay.addWidget(
            label('Runtime key chỉ lưu trong Windows Credential Manager. Không đưa key vào Chat.', 'Muted', True)
        )
        form = QFormLayout()
        form.setVerticalSpacing(12)
        self.tunnel = QLineEdit(controller.config().get('tunnel_id', ''))
        self.tunnel.setPlaceholderText('tunnel_…')
        form.addRow('Tunnel ID', self.tunnel)
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText('Để trống nếu giữ key đã lưu')
        form.addRow('Runtime API key', self.key)
        lay.addLayout(form)
        lay.addWidget(label('Quyền cần thiết: Tunnels Read + Use. Không cần key cho model API.', 'Fine', True))
        settings = controller.settings()
        self.motion = QCheckBox('Bật chuyển động nhẹ')
        self.motion.setChecked(settings.get('motion', True))
        self.auto = QCheckBox('Tự kết nối khi mở ứng dụng')
        self.auto.setChecked(settings.get('autoconnect', False))
        lay.addWidget(self.motion)
        lay.addWidget(self.auto)
        self.error = label('', 'Warning', True)
        lay.addWidget(self.error)
        cancel = MotionButton('Hủy')
        cancel.clicked.connect(self.reject)
        save = MotionButton('Lưu cục bộ', 'check', 'primary')
        save.clicked.connect(self.save)
        lay.addLayout(row(None, cancel, save))

    def save(self):
        try:
            self.controller.configure(self.tunnel.text(), self.key.text())
            self.key.clear()
            self.controller.save_settings({'motion': self.motion.isChecked(), 'autoconnect': self.auto.isChecked()})
            QApplication.instance().setProperty('motion', self.motion.isChecked())
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
        self.payload = json.loads(request['payload'])
        self.setWindowTitle('Xem chính xác thay đổi · phê duyệt cục bộ')
        self.resize(980, 730)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(13)
        p = self.payload
        self.is_write = request['kind'] == 'write'
        self.is_desktop = request['kind'] == 'desktop'
        self.is_media = request['kind'] == 'media'
        lay.addWidget(
            label(
                (
                    'Thay đổi file'
                    if self.is_write
                    else 'Phiên chuột/phím trong Sandbox'
                    if self.is_desktop
                    else 'Tải video bằng FFmpeg trên Windows'
                    if self.is_media
                    else 'Chạy mã trong Windows Sandbox'
                )
                + ' / '
                + p['project'],
                'H2',
            )
        )
        summary = label(
            (p.get('path') or p.get('task') or p.get('language', '')) + '\nThư mục: ' + p['root'], 'Muted', True
        )
        summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(summary)
        details = label('Request: ' + request['id'] + '\nSHA256: ' + request['hash'], 'Fine', True)
        details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(details)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        self.full = QPlainTextEdit()
        self.full.setReadOnly(True)
        if self.is_write:
            self.preview.setPlainText(p['diff'])
            self.full.setPlainText(p['text'])
            tabs.addTab(self.preview, 'So sánh thay đổi')
            tabs.addTab(self.full, 'Toàn bộ nội dung mới')
            self.highlight = DiffHighlighter(self.preview.document())
        elif self.is_media:
            self.preview.setPlainText(
                'NGUỒN HTTPS: '
                + p['url']
                + '\nTÊN TỆP: '
                + p['filename']
                + '\nTHƯ MỤC: '
                + p['root']
                + '\nHOST ĐƯỢC PHÉP: '
                + p['host']
                + '\n\nChỉ tải video HLS không mã hóa về MP4 bằng FFmpeg sau khi được duyệt tại máy Windows. Không ghi đè file đã tồn tại. Giữ ứng dụng mở cho đến khi hoàn thành.'
            )
            self.full.setPlainText(json.dumps(p, indent=2, ensure_ascii=False))
            tabs.addTab(self.preview, 'Xem nguồn và đích')
            tabs.addTab(self.full, 'Chi tiết yêu cầu')
        elif self.is_desktop:
            self.preview.setPlainText(
                'NHIỆM VỤ ĐƯỢC CẤP QUYỀN\n'
                + p['task']
                + '\n\nThời lượng: '
                + str(p['duration_seconds'])
                + ' giây (+ tối đa 90 giây khởi động VM).\n\n'
                + json.dumps(p['permissions'], indent=2, ensure_ascii=False)
                + '\n\nSHA256 bộ điều khiển guest: '
                + p['guest_sha256']
            )
            self.full.setPlainText(json.dumps(p['manifest'], indent=2, ensure_ascii=False))
            tabs.addTab(self.preview, 'Phạm vi chuột / bàn phím')
            tabs.addTab(self.full, 'Snapshot nguồn chỉ đọc')
        else:
            self.preview.setPlainText(p['script'])
            self.full.setPlainText(json.dumps(p['manifest'], indent=2, ensure_ascii=False))
            tabs.addTab(self.preview, 'Script chính xác')
            tabs.addTab(self.full, 'Snapshot nguồn')
        lay.addWidget(
            label(
                'Sửa file yêu cầu phê duyệt. Code/desktop chạy trong Sandbox; riêng tải video dùng FFmpeg trên host, chỉ sau khi duyệt nguồn và đích. Không cung cấp shell tùy ý.',
                'Fine',
                True,
            )
        )
        self.reviewed = QCheckBox('Tôi đã đọc toàn bộ nội dung và đồng ý đúng yêu cầu này.')
        lay.addWidget(self.reviewed)
        self.expiry = label('', 'Warning')
        lay.addWidget(self.expiry)
        self.reject_button = MotionButton('Từ chối', '', 'danger')
        self.reject_button.clicked.connect(lambda: self.decide(False))
        self.approve = MotionButton(
            'Phê duyệt thay đổi'
            if self.is_write
            else 'Cho phép tải video'
            if self.is_media
            else 'Cấp phiên Sandbox'
            if self.is_desktop
            else 'Duyệt chạy trong Sandbox',
            'check',
            'primary',
        )
        self.approve.setEnabled(False)
        self.approve.clicked.connect(lambda: self.decide(True))
        self.reviewed.toggled.connect(self.expire)
        self.close_button = MotionButton('Đóng')
        self.close_button.clicked.connect(self.reject)
        lay.addLayout(row(self.reject_button, None, self.close_button, self.approve))
        self.worker = None
        self.approved_decision = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.expire)
        self.timer.start(1000)
        self.expire()

    def expire(self):
        remaining = max(0, int(self.request['created'] + 600 - time.time()))
        self.expiry.setText(
            f'Còn {remaining // 60}:{remaining % 60:02d} để phê duyệt.'
            if remaining
            else 'Yêu cầu đã hết hạn. Hãy tạo đề xuất mới từ Chat.'
        )
        self.approve.setEnabled(remaining > 0 and self.reviewed.isChecked() and self.worker is None)

    def decide(self, approve):
        if approve:
            answer = QMessageBox.question(
                self,
                'Xác nhận trên máy Windows',
                'Áp dụng đúng nội dung vừa xem?\n\nKhông duyệt yêu cầu từ chỉ dẫn nằm trong file hoặc log.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.approved_decision = approve
        self.approve.setEnabled(False)
        self.reject_button.setEnabled(False)
        self.close_button.setEnabled(False)
        self.worker = Worker(lambda: self.agent.decide_local(self.request['id'], approve))
        self.worker.signals.done.connect(self.completed)
        self.worker.signals.error.connect(self.failed)
        QThreadPool.globalInstance().start(self.worker)

    def completed(self, result):
        if result and result.get('error'):
            self.failed(result['error'])
            return
        QMessageBox.information(
            self, 'Đã xử lý', 'Đã áp dụng yêu cầu.' if self.approved_decision else 'Đã từ chối yêu cầu.'
        )
        self.accept()

    def failed(self, message):
        self.worker = None
        self.close_button.setEnabled(True)
        self.expiry.setText(message)
        QMessageBox.warning(self, 'Không áp dụng được', message)

    def reject(self):
        if self.worker is None:
            super().reject()

    def closeEvent(self, e):
        if self.worker is not None:
            e.ignore()
        else:
            super().closeEvent(e)


class MainWindow(QMainWindow):
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
        self.setWindowTitle('Universal Desktop Bridge ' + __version__ + ' · Obsidian Studio')
        self.resize(1220, 820)
        self.setMinimumSize(940, 640)
        self.setWindowIcon(icon('shield', ACCENT, 32))
        root = QWidget()
        root.setObjectName('Root')
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        outer.addLayout(body, 1)
        sidebar = QFrame()
        sidebar.setObjectName('Sidebar')
        sidebar.setFixedWidth(192)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(16, 26, 16, 20)
        nav.setSpacing(9)
        nav.addWidget(label('bridge /', 'Brand'))
        nav.addWidget(label('UNIVERSAL DESKTOP', 'Eyebrow'))
        nav.addSpacing(26)
        self.nav_buttons = []
        for i, (text, glyph) in enumerate(
            (
                ('Dự án', 'projects'),
                ('Quyền AI', 'review'),
                ('Tiến trình', 'activity'),
                ('Bảo vệ', 'shield'),
                ('Kết nối', 'settings'),
                ('Máy tính', 'activity'),
            )
        ):
            btn = MotionButton(text, glyph, 'nav')
            btn.setMinimumHeight(44)
            btn.clicked.connect(lambda checked=False, index=i: self.navigate(index))
            nav.addWidget(btn)
            self.nav_buttons.append(btn)
        nav.addStretch()
        protected = label('Checkpoint & dữ liệu\nđược bảo vệ', 'Muted', True)
        nav.addWidget(protected)
        nav.addSpacing(12)
        nav.addWidget(label('OBSIDIAN STUDIO', 'Eyebrow'))
        nav.addWidget(label('Agent ' + __version__, 'Fine'))
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
        foot.setObjectName('Footer')
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(22, 9, 22, 9)
        self.footer_status = label('MCP riêng tư · Quyền tự động theo dự án', 'Fine')
        fl.addWidget(self.footer_status)
        fl.addStretch()
        self.footer_project = label('Chưa chọn dự án', 'Fine')
        fl.addWidget(self.footer_project)
        outer.addWidget(foot)
        # Keep stacked pages opaque. A graphics effect on the entire stack can
        # retain stale page pixels on Windows when pages change or resize.
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(1500)
        self.navigate(0)
        self.refresh()

    def page(self, title, subtitle):
        p = QWidget()
        p.setObjectName('Page')
        layout = QVBoxLayout(p)
        layout.setContentsMargins(26, 24, 26, 22)
        layout.setSpacing(16)
        header = QWidget()
        header.setObjectName('PageHeader')
        hl = QVBoxLayout(header)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(8)
        h = label(title, 'H1', True)
        h.setMinimumHeight(40)
        hl.addWidget(h)
        hl.addWidget(label(subtitle, 'Subtitle', True))
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
            'Không gian làm việc',
            'AI tự thao tác trong dự án đã cấp quyền. Checkpoint và dữ liệu nhạy cảm luôn bị chặn.',
        )
        top = row(label('DỰ ÁN / WINDOWS', 'Eyebrow'), None)
        self.status_label = label('●  Chưa kết nối', 'Badge')
        top.addWidget(self.status_label)
        lay.insertLayout(0, top)
        self.band = StatusBand()
        band = QHBoxLayout(self.band)
        band.setContentsMargins(22, 20, 22, 20)
        info = QVBoxLayout()
        info.setSpacing(6)
        info.addWidget(label('Kết nối với ChatGPT Chat', 'H2'))
        self.connection_hint = label('Private MCP tunnel · không mở cổng công khai', 'Muted', True)
        info.addWidget(self.connection_hint)
        band.addLayout(info, 1)
        self.configure_button = MotionButton('Cấu hình', 'settings')
        self.configure_button.clicked.connect(self.configure)
        band.addWidget(self.configure_button)
        self.connect_button = MotionButton('Kết nối', 'power', 'primary')
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
        add = MotionButton('Thêm dự án', 'plus')
        add.clicked.connect(self.add_project)
        ll.addLayout(row(label('Dự án nguồn', 'H2'), None, add))
        self.projects = self.tree(['Dự án', 'Quyền truy cập'])
        self.projects.setMinimumHeight(110)
        self.projects.setColumnWidth(0, 180)
        self.projects.itemSelectionChanged.connect(self.project_selected)
        self.projects.itemDoubleClicked.connect(lambda *_: self.open_source())
        ll.addWidget(self.projects, 1)
        self.project_path = QLineEdit()
        self.project_path.setObjectName('ProjectPath')
        self.project_path.setReadOnly(True)
        self.project_path.setMinimumWidth(0)
        self.project_path.setPlaceholderText('Thư mục nguồn')
        ll.addWidget(self.project_path)
        self.project_access = label('Chọn dự án để xem quyền truy cập.', 'Fine', True)
        ll.addWidget(self.project_access)
        self.sandbox_auto = QCheckBox('AI tự làm việc trong Sandbox')
        self.sandbox_auto.setToolTip(
            'Cho dự án này tự mở desktop và chạy script trong VM. Không cấp quyền ghi file gốc hoặc điều khiển host. Bỏ chọn để thu hồi và dừng phiên tự động.'
        )
        self.sandbox_auto.toggled.connect(self.set_sandbox_autonomy)
        ll.addWidget(self.sandbox_auto)
        self.source_auto = QCheckBox('AI tự sửa mã nguồn (không duyệt từng file)')
        self.source_auto.setToolTip(
            'Chỉ trong dự án đã đăng ký có quyền ghi. Không sửa checkpoint, dữ liệu, bí mật, file cấu hình quan trọng. Mỗi lần sửa có backup + SHA256; có thể tắt ngay.'
        )
        self.source_auto.toggled.connect(self.set_source_autonomy)
        ll.addWidget(self.source_auto)
        view = MotionButton('Xem nguồn', 'file')
        view.clicked.connect(self.open_source)
        revoke = MotionButton('Thu hồi', '', 'danger')
        revoke.clicked.connect(self.revoke)
        ll.addLayout(row(view, revoke, None))
        split.addWidget(left)
        rail = panel()
        rail.setMinimumWidth(236)
        rl = QVBoxLayout(rail)
        rl.setContentsMargins(18, 18, 18, 18)
        rl.setSpacing(13)
        rl.addWidget(label('AN TOÀN & NGOẠI LỆ', 'Eyebrow'))
        self.pending_title = label('Không có yêu cầu chờ duyệt', 'H2', True)
        rl.addWidget(self.pending_title)
        self.pending_hint = label(
            'Trong phạm vi đã cấp quyền, AI sửa tự động; trường hợp ngoại lệ hiện tại đây.', 'Muted', True
        )
        rl.addWidget(self.pending_hint)
        self.pending_code = QPlainTextEdit()
        self.pending_code.setReadOnly(True)
        self.pending_code.setMaximumHeight(155)
        self.pending_code.setPlaceholderText('Chưa có thay đổi mới.')
        rl.addWidget(self.pending_code)
        self.rail_highlight = DiffHighlighter(self.pending_code.document())
        self.review_button = MotionButton('Xem đề xuất', 'review')
        self.review_button.clicked.connect(self.review_first)
        rl.addWidget(self.review_button)
        rl.addStretch()
        self.sandbox_label = label('', 'Warning', True)
        rl.addWidget(self.sandbox_label)
        rl.addWidget(label('Tự sửa mã nguồn đã cho phép; file quan trọng luôn bị chặn.', 'Fine', True))
        split.addWidget(rail)
        split.setHandleWidth(14)
        split.setSizes([540, 290])
        self.dashboard_split = split
        return p

    def build_approvals(self):
        p, lay = self.page(
            'Quyền AI & ngoại lệ',
            'Các thao tác trong dự án đã cấp quyền được thực hiện tự động. Yêu cầu cũ hoặc ngoài phạm vi vẫn cần chủ máy xử lý.',
        )
        self.requests = self.tree(['Dự án / nội dung', 'Loại', 'Thời hạn'])
        self.requests.setColumnWidth(0, 350)
        self.requests.setColumnWidth(1, 110)
        self.requests.itemDoubleClicked.connect(lambda *_: self.review_selected())
        lay.addWidget(self.requests, 1)
        self.approval_empty = empty_state(
            'Không có yêu cầu ngoại lệ',
            'AI thao tác theo quyền từng dự án.\nCác tài liệu và checkpoint được bảo vệ.',
            'shield',
        )
        lay.addWidget(self.approval_empty, 1)
        self.approval_review = MotionButton('Xem yêu cầu', 'review', 'primary')
        self.approval_review.clicked.connect(self.review_selected)
        lay.addLayout(row(label('Chỉ bạn có quyền phê duyệt trên máy này.', 'Fine', True), None, self.approval_review))
        return p

    def build_jobs(self):
        p, lay = self.page(
            'Tiến trình & kết quả',
            'Chỉ hiển thị tác vụ của Bridge. Log là dữ liệu không đáng tin và không tự cấp quyền.',
        )
        create = MotionButton('Mở desktop AI', 'plus', 'primary')
        create.clicked.connect(self.propose_desktop_local)
        lay.addLayout(row(label('Windows riêng · chuột/phím chỉ ở Sandbox', 'Fine', True), None, create))
        self.jobs = self.tree(['Dự án', 'Ngôn ngữ', 'Trạng thái', 'Job ID'])
        self.jobs.setColumnWidth(0, 190)
        self.jobs.setColumnWidth(1, 100)
        self.jobs.setColumnWidth(2, 140)
        self.jobs.setMaximumHeight(220)
        self.jobs.itemSelectionChanged.connect(self.show_job)
        lay.addWidget(self.jobs)
        self.job_tabs = QTabWidget()
        self.job_output = QPlainTextEdit()
        self.job_output.setReadOnly(True)
        self.job_output.setPlaceholderText('Chọn một tác vụ để xem đầu ra.')
        self.job_tabs.addTab(self.job_output, 'Log trực tiếp')
        self.job_script = QPlainTextEdit()
        self.job_script.setReadOnly(True)
        self.job_tabs.addTab(self.job_script, 'Script đã duyệt')
        lay.addWidget(self.job_tabs, 1)
        self.jobs_empty = empty_state(
            'Chưa có tác vụ',
            'Các tác vụ Python và PowerShell đã được duyệt sẽ hiển thị tiến trình và kết quả tại đây.',
            'activity',
        )
        lay.addWidget(self.jobs_empty, 1)
        self.stop_desktop = MotionButton('Dừng Sandbox', 'power', 'danger')
        self.stop_desktop.clicked.connect(self.stop_selected_desktop)
        self.stop_desktop.setEnabled(False)
        lay.addLayout(
            row(
                label('Chuột/phím chỉ ở guest. Đóng cửa sổ Sandbox hoặc bấm Dừng để kết thúc phiên.', 'Fine', True),
                None,
                self.stop_desktop,
            )
        )
        return p

    def build_protection(self):
        p, lay = self.page(
            'Vùng được bảo vệ',
            'Thư mục, checkpoint, mô hình, dữ liệu và bí mật luôn nằm ngoài quyền truy cập tự động của AI.',
        )
        self.protected = self.tree(['Đường dẫn được chặn'])
        lay.addWidget(self.protected, 1)
        note = panel()
        nl = QVBoxLayout(note)
        nl.setContentsMargins(20, 18, 20, 18)
        nl.addWidget(label('Bảo vệ nhiều lớp', 'H2'))
        nl.addWidget(
            label(
                'Chặn checkpoint, weights, datasets, credential và đường dẫn vượt dự án. Chặn symlink, junction và hardlink. Sửa mã nguồn đã cấp quyền sẽ tự động, kiểm tra hash và backup.',
                'Muted',
                True,
            )
        )
        lay.addWidget(note)
        protect = MotionButton('Bảo vệ thêm thư mục', 'shield', 'primary')
        protect.clicked.connect(self.protect)
        lay.addLayout(row(None, protect))
        return p

    def build_connection(self):
        p, lay = self.page(
            'Kết nối & cài đặt',
            'Kết nối ChatGPT Chat qua tunnel riêng. API key không xuất hiện trong giao diện hay log.',
        )
        card = panel()
        cl = QVBoxLayout(card)
        cl.setContentsMargins(23, 23, 23, 23)
        cl.setSpacing(15)
        cl.addWidget(label('Secure MCP Tunnel', 'H2'))
        self.detail_status = label('', 'Muted', True)
        cl.addWidget(self.detail_status)
        self.media_auto = QCheckBox('AI tự tải HLS → MP4 từ tên miền đã cho phép (không duyệt từng video)')
        self.media_auto.setToolTip(
            'Chỉ HTTPS từ state/media-allowed-hosts.json, file MP4 ở Downloads, không ghi đè; giới hạn kích thước và thời lượng. Không mở host shell.'
        )
        self.media_auto.toggled.connect(self.set_media_autonomy)
        cl.addWidget(self.media_auto)
        config = MotionButton('Cài đặt kết nối', 'settings')
        config.clicked.connect(self.configure)
        plugin = MotionButton('Mở plugin ChatGPT', 'external')
        plugin.clicked.connect(lambda: self.open_url(PLUGIN_URL))
        cl.addLayout(row(config, plugin, None))
        lay.addWidget(card)
        local = panel()
        ll = QVBoxLayout(local)
        ll.setContentsMargins(23, 23, 23, 23)
        ll.setSpacing(14)
        ll.addWidget(label('MCP cục bộ · chẩn đoán nâng cao', 'H2'))
        ll.addWidget(
            label(
                '127.0.0.1:17843/mcp · bearer-protected. Không cần bật mục này khi dùng tuyến tunnel STDIO.',
                'Muted',
                True,
            )
        )
        self.local_button = MotionButton('Bật MCP cục bộ', 'power')
        self.local_button.clicked.connect(self.toggle_local)
        ll.addLayout(row(self.local_button, None))
        lay.addWidget(local)
        help_text = label(
            'Sau khi Windows khởi động lại: mở Bridge và Kết nối. Trong Chat mới, chọn @Universal Desktop Bridge. Nếu cần chạy mã, gửi đề xuất từ Chat rồi duyệt script chính xác ở Phê duyệt.\n\nBản cài không gọi model API và không tự mua hay nâng cấp dịch vụ. ChatGPT và tunnel vẫn có điều kiện, hạn mức riêng.',
            'Muted',
            True,
        )
        lay.addWidget(help_text)
        lay.addStretch()
        return p

    def build_host(self):
        p, lay = self.page(
            'Máy tính · chạy nền',
            'Phiên trình duyệt riêng và cửa sổ soạn thảo riêng trên Windows. Không dùng chuột/phím hệ thống hoặc tab hiện có.',
        )
        self.host_enabled = QCheckBox('Cho phép UDB mở phiên nền riêng trên máy này')
        self.host_enabled.toggled.connect(self.set_host_enabled)
        lay.addWidget(self.host_enabled)
        self.host_resource = label('Đang lấy số liệu CPU/RAM…', 'H2', True)
        lay.addWidget(self.host_resource)
        self.host_pressure = label('', 'Muted', True)
        lay.addWidget(self.host_pressure)
        lay.addWidget(
            label(
                'Tối đa 2 phiên (1 trình duyệt + 1 cửa sổ), 4 tab; một thao tác tại một thời điểm. CPU ≥85%, RAM ≥85% hoặc RAM trống <1,5 GB: chặn mở thêm và tạm đóng băng các trang UDB.',
                'Muted',
                True,
            )
        )
        self.host_sessions = QPlainTextEdit()
        self.host_sessions.setReadOnly(True)
        lay.addWidget(self.host_sessions, 1)
        browser = MotionButton('Mở trình duyệt nền', 'plus')
        browser.clicked.connect(lambda: self.start_host_local('browser'))
        workspace = MotionButton('Mở cửa sổ riêng', 'plus')
        workspace.clicked.connect(lambda: self.start_host_local('workspace'))
        stop = MotionButton('Dừng toàn bộ phiên UDB', 'power', 'danger')
        stop.clicked.connect(self.stop_host_local)
        lay.addLayout(row(browser, workspace, None, stop))
        lay.addWidget(
            label(
                'Trình duyệt nền không hiện thành tab trên Chrome bạn đang dùng; có thể đọc/chụp trang qua công cụ host. Không dùng tài khoản đăng nhập sẵn của bạn. Cửa sổ riêng hiện tại hỗ trợ văn bản; chưa điều khiển ứng dụng bất kỳ.',
                'Fine',
                True,
            )
        )
        return p

    def set_host_enabled(self, value):
        from .host_client import atomic_json

        self.guarded(lambda: atomic_json(self.agent.state / 'host-settings.json', {'enabled': bool(value)}))

    def start_host_local(self, kind):
        from .host_client import command

        self.run_background(
            lambda: command(
                self.agent, 'start', kind=kind, task='Phiên riêng do chủ máy mở trong UDB', duration_seconds=900
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
            QMessageBox.warning(self, 'Bridge · không thực hiện được', str(e))

    def run_background(self, fn, done=None):
        if self.busy:
            return
        self.busy = True
        self.connect_button.setEnabled(False)
        self.local_button.setEnabled(False)
        self.configure_button.setEnabled(False)
        self.status_label.setText('●  Đang xử lý…')
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
            QMessageBox.warning(self, 'Không kết nối được', message)

        worker.signals.done.connect(complete, Qt.ConnectionType.QueuedConnection)
        worker.signals.error.connect(failed, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(worker)

    def toggle_tunnel(self):
        if self.controller.tunnel_wanted:
            self.run_background(lambda: self.controller.stop_process('tunnel'))
        else:
            self.run_background(self.controller.start_tunnel)

    def toggle_local(self):
        self.run_background(
            lambda: self.controller.stop_process('mcp')
            if self.controller.alive('mcp')
            else self.controller.start_local()
        )

    def configure(self):
        if not self.busy:
            ConnectionDialog(self.controller, self).exec()

    def project_selected(self):
        selected = self.projects.selectedItems()
        self.selected_project = selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None
        self.sandbox_auto.blockSignals(True)
        self.sandbox_auto.setEnabled(bool(self.selected_project))
        automatic = bool(
            self.selected_project
            and self.agent.sandbox_authorization(self.selected_project['id'], self.selected_project['root'])
        )
        self.sandbox_auto.setChecked(automatic)
        self.sandbox_auto.blockSignals(False)
        self.source_auto.blockSignals(True)
        source_granted = bool(
            self.selected_project
            and self.agent.source_autonomy_authorization(self.selected_project['id'], self.selected_project['root'])
        )
        self.source_auto.setEnabled(bool(self.selected_project and self.selected_project['writable']))
        self.source_auto.setChecked(source_granted)
        self.source_auto.blockSignals(False)
        if self.selected_project:
            self.footer_project.setText('Dự án: ' + self.selected_project['id'])
            self.project_path.setText(self.selected_project['root'])
            self.project_path.setCursorPosition(0)
            self.project_path.setToolTip(self.selected_project['root'])
            self.project_access.setText(
                (
                    'AI tự sửa mã nguồn hợp lệ, có backup.'
                    if source_granted
                    else 'Được ghi: bật AI tự sửa để không phải duyệt.'
                    if self.selected_project['writable']
                    else 'File gốc: chỉ đọc.'
                )
                + (' Sandbox: tự động.' if automatic else ' Sandbox: chưa cấp quyền tự động.')
            )
        else:
            self.footer_project.setText('Chưa chọn dự án')
            self.project_path.clear()
            self.project_path.setToolTip('')
            self.project_access.setText('Thêm thư mục nguồn để bắt đầu.')

    def set_sandbox_autonomy(self, enabled):
        if self.selected_project:
            self.guarded(lambda: self.agent.set_sandbox_autonomy_local(self.selected_project['id'], enabled))
        self.project_selected()
        self.refresh()

    def set_source_autonomy(self, enabled):
        if not self.selected_project:
            return
        if enabled:
            ok = QMessageBox.question(
                self,
                'Bật AI tự sửa mã nguồn',
                'Cấp quyền ghi tự động cho AI trong dự án ' + self.selected_project['id'] + '?\n'
                'Không cần phê duyệt từng file. File quan trọng vẫn bị chặn; mỗi lần sửa lưu backup.\n'
                'Chọn No để giữ quyền hiện tại.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if ok != QMessageBox.StandardButton.Yes:
                self.project_selected()
                return
        self.guarded(lambda: self.agent.set_source_autonomy_local(self.selected_project['id'], enabled))
        self.project_selected()
        self.refresh()

    def set_media_autonomy(self, enabled):
        if enabled:
            ok = QMessageBox.question(
                self,
                'Cho phép tải video tự động',
                'Cấp quyền tải HLS HTTPS từ các tên miền được chủ máy cho phép, '
                'chỉ lưu MP4 trong Downloads, không hỏi từng video?\n'
                'Có thể tốn băng thông/dung lượng. Không cho phép chạy lệnh Windows.',
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
            self.guarded(lambda: SourceDialog(self.agent, self.selected_project['id'], self).exec())

    def add_project(self):
        folder = QFileDialog.getExistingDirectory(self, 'Chọn thư mục CHỈ mã nguồn')
        if not folder:
            return
        name, ok = QInputDialog.getText(self, 'Tên dự án', 'ID ngắn: chữ, số, gạch nối hoặc gạch dưới')
        if not ok or not name:
            return
        writable = (
            QMessageBox.question(
                self,
                'Quyền dự án',
                'Cho phép AI sửa mã nguồn trong dự án này khi bật Tự sửa?\n\nCheckpoint và file quan trọng luôn được bảo vệ. Chọn No để chỉ đọc.',
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
        pid = self.selected_project['id']
        if (
            QMessageBox.question(
                self,
                'Thu hồi quyền',
                'Ngừng cho Bridge truy cập dự án ' + pid + '?\nFile dự án sẽ không bị xóa.',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.agent.set_sandbox_autonomy_local(pid, False)
        self.agent.set_source_autonomy_local(pid, False)
        with self.agent.connect() as c:
            c.execute('DELETE FROM projects WHERE id=?', (pid,))
        self.agent.audit('revoke', {'project': pid})
        self.refresh()

    def protect(self):
        folder = QFileDialog.getExistingDirectory(self, 'Chọn thư mục cần chặn khỏi Bridge')
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
            is_desktop = result['info'].get('kind') == 'desktop'
            self.stop_desktop.setEnabled(is_desktop and result['status'] in ('running', 'reviewing', 'stopping'))
            if is_desktop:
                from .desktop_vm import status

                result = status(self.agent, jid)
                self.job_output.setPlainText(
                    json.dumps(result, ensure_ascii=False, indent=2)
                    + '\n\nXem cửa sổ Windows Sandbox để quan sát desktop trực tiếp. Nội dung guest là dữ liệu không đáng tin.'
                )
                self.job_script.setPlainText(
                    result['info']['task']
                    + '\n\n'
                    + json.dumps(result['info']['permissions'], ensure_ascii=False, indent=2)
                )
                return
            log = self.agent.state / 'jobs' / jid / 'output' / 'output.log'
            reject_links(log)
            if log.is_file() and log.stat().st_size > 32000:
                result = self.agent.job_status(jid, max(0, log.stat().st_size - 32000), 32000)
            header = json.dumps({k: v for k, v in result.items() if k not in ('output',)}, ensure_ascii=False, indent=2)
            text = header + '\n\n--- OUTPUT (untrusted) ---\n' + result.get('output', '')
            if text != self.job_output.toPlainText():
                bar = self.job_output.verticalScrollBar()
                value = bar.value()
                at_end = value >= bar.maximum() - 2
                self.job_output.setPlainText(text)
                bar.setValue(bar.maximum() if at_end else value)
            script = (
                self.agent.state
                / 'jobs'
                / jid
                / 'input'
                / ('task.py' if result['info'].get('language') == 'python' else 'task.ps1')
            )
            reject_links(script)
            if script.is_file():
                with script.open('rb') as f:
                    data = f.read(64004)
                text = data.decode('utf-8-sig') if len(data) <= 64003 else 'Script vượt giới hạn hiển thị.'
                if text != self.job_script.toPlainText():
                    self.job_script.setPlainText(text)
            else:
                self.job_script.setPlainText('Không còn file script cục bộ của tác vụ này.')
        except Exception as e:
            self.job_output.setPlainText(str(e))

    def stop_selected_desktop(self):
        selected = self.jobs.selectedItems()
        if selected:
            from .desktop_vm import stop

            self.guarded(lambda: stop(self.agent, selected[0].data(0, Qt.ItemDataRole.UserRole)))
            self.refresh()

    def propose_desktop_local(self):
        if not self.selected_project:
            QMessageBox.information(self, 'Chọn dự án', 'Chọn dự án nguồn ở trang Dự án trước.')
            return
        task, ok = QInputDialog.getMultiLineText(
            self,
            'Nhiệm vụ cho desktop AI',
            'AI được làm gì trong Sandbox? Phiên 10 phút, mạng/clipboard tắt.',
            'Mở UDB Sandbox Pad, gõ UDB_SANDBOX_INPUT_OK, bấm Confirm và chụp màn hình kiểm tra. Chỉ thao tác trong guest Windows Sandbox.',
        )
        if ok and task.strip():
            response = self.guarded(lambda: self.agent.propose_desktop(self.selected_project['id'], task.strip(), 600))
            self.refresh()
            if response:
                if response['status'] == 'queued_sandbox':
                    self.navigate(2)
                else:
                    self.review(response['request_id'])

    def refresh(self):
        if self._closing:
            return
        try:
            projects = self.agent.projects()
            signature = json.dumps(projects)
            if signature != self.last_signature:
                self.last_signature = signature
                selected = self.selected_project['id'] if self.selected_project else None
                self.projects.clear()
                for proj in projects:
                    item = QTreeWidgetItem(
                        [
                            proj['id'],
                            'AI tự sửa'
                            if proj['id'] in self.agent.source_autonomy_projects()
                            else 'Ghi có giới hạn'
                            if proj['writable']
                            else 'Chỉ đọc',
                        ]
                    )
                    item.setIcon(0, icon('folder', MUTED))
                    item.setData(0, Qt.ItemDataRole.UserRole, proj)
                    item.setToolTip(0, proj['root'])
                    self.projects.addTopLevelItem(item)
                    if proj['id'] == selected:
                        self.projects.setCurrentItem(item)
                if not self.projects.selectedItems() and projects:
                    self.projects.setCurrentItem(self.projects.topLevelItem(0))
                self.project_selected()
            self.project_selected()
            from .host_client import status as host_status

            hs = host_status(self.agent)
            rs = hs['resources']
            self.host_enabled.blockSignals(True)
            self.host_enabled.setChecked(hs['enabled'])
            self.host_enabled.blockSignals(False)
            cpu = rs.get('cpu_percent')
            ram = rs.get('ram_percent')
            self.host_resource.setText(
                'CPU '
                + (str(cpu) + '%' if cpu is not None else '—')
                + '   ·   RAM '
                + (str(ram) + '%' if ram is not None else '—')
                + '   ·   Trống '
                + str(rs.get('available_mib', 0))
                + ' MB'
            )
            self.host_pressure.setText(
                'Tạm dừng nhận tác vụ: máy bận hoặc đang chờ số liệu.'
                if not rs.get('admission_ready')
                else 'Đủ tài nguyên để nhận tác vụ; kiểm tra lại trước mỗi lần mở.'
            )
            self.host_sessions.setPlainText(
                '\n\n'.join(
                    x['kind']
                    + ' · '
                    + x['session_id']
                    + '\n'
                    + x['task']
                    + '\nTab: '
                    + str(x['tabs'])
                    + ' · Tiến trình: '
                    + str(x.get('processes', 0))
                    + ' · Commit cao nhất: '
                    + str(x.get('peak_commit_mib', 0))
                    + ' MB'
                    for x in hs['sessions']
                )
                or 'Chưa có phiên nền của UDB.'
            )
            self.media_auto.blockSignals(True)
            self.media_auto.setChecked(self.agent.media_autonomy())
            self.media_auto.blockSignals(False)
            rows = self.agent.pending_local()
            self.pending_rows = {r['id']: r for r in rows}
            signature = tuple(self.pending_rows)
            if signature != self.last_pending:
                self.last_pending = signature
                self.requests.clear()
                for request in rows:
                    p = json.loads(request['payload'])
                    item = QTreeWidgetItem(
                        [
                            p['project'] + ' / ' + p.get('path', p.get('task', p.get('language', ''))),
                            'Sửa file'
                            if request['kind'] == 'write'
                            else 'Chuột/phím'
                            if request['kind'] == 'desktop'
                            else 'Sandbox',
                            '',
                        ]
                    )
                    item.setData(0, Qt.ItemDataRole.UserRole, request['id'])
                    self.requests.addTopLevelItem(item)
                if rows:
                    self.requests.setCurrentItem(self.requests.topLevelItem(0))
                if rows:
                    p = json.loads(rows[0]['payload'])
                    self.pending_title.setText(p.get('path', p.get('language', 'Phiên chuột/phím')))
                    self.pending_hint.setText(
                        p['project']
                        + ' · '
                        + (
                            'Thay đổi file'
                            if rows[0]['kind'] == 'write'
                            else 'Chuột/phím trong Sandbox'
                            if rows[0]['kind'] == 'desktop'
                            else 'Tải video FFmpeg'
                            if rows[0]['kind'] == 'media'
                            else 'Chạy trong Sandbox'
                        )
                    )
                    self.pending_code.setPlainText(p.get('diff', p.get('script', p.get('task', '')))[:8000])
                else:
                    self.pending_title.setText('Không có yêu cầu chờ duyệt')
                    self.pending_hint.setText('Đề xuất sửa và chạy mã từ Chat sẽ xuất hiện tại đây.')
                    self.pending_code.clear()
            for i, r in enumerate(rows):
                self.requests.topLevelItem(i).setText(2, str(max(0, int(r['created'] + 600 - time.time()))) + ' giây')
            self.pending_code.setVisible(bool(rows))
            self.requests.setVisible(bool(rows))
            self.approval_empty.setVisible(not rows)
            self.approval_review.setEnabled(bool(rows))
            self.review_button.setEnabled(bool(rows))
            self.nav_buttons[1].setText('Quyền AI' + (f'  {len(rows)}' if rows else ''))
            with self.agent.connect() as c:
                protected = [r[0] for r in c.execute('SELECT root FROM protected ORDER BY root')]
                jobs = [dict(r) for r in c.execute('SELECT * FROM jobs ORDER BY rowid DESC LIMIT 30')]
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
                    info = json.loads(j['info'])
                    status_text = {
                        'running': 'Đang chạy',
                        'reviewing': 'Đã xong · đang xem',
                        'completed': 'Hoàn tất',
                        'failed': 'Lỗi',
                        'timeout': 'Hết thời gian',
                        'output_limit': 'Log vượt giới hạn',
                        'stopping': 'Đang dừng',
                        'stopped': 'Đã dừng',
                    }.get(j['status'], j['status'])
                    item = QTreeWidgetItem(
                        [
                            info.get('project', ''),
                            info.get('language', 'Desktop' if info.get('kind') == 'desktop' else ''),
                            status_text,
                            j['id'],
                        ]
                    )
                    item.setData(0, Qt.ItemDataRole.UserRole, j['id'])
                    self.jobs.addTopLevelItem(item)
                    if j['id'] == old:
                        self.jobs.setCurrentItem(item)
                if jobs and not self.jobs.selectedItems():
                    self.jobs.setCurrentItem(self.jobs.topLevelItem(0))
            self.jobs.setVisible(bool(jobs))
            self.job_tabs.setVisible(bool(jobs))
            self.jobs_empty.setVisible(not jobs)
            if self.jobs.selectedItems():
                self.show_job()
            status = self.controller.status()
            ready = status['ready']
            alive = status['tunnel_running']
            wanted = status['tunnel_wanted']
            text = (
                'Đã sẵn sàng'
                if ready
                else (
                    'Đang kết nối…'
                    if alive or status['connecting']
                    else f"Thử lại sau {status['retry_in_seconds']:.0f} giây"
                    if wanted
                    else 'Chưa kết nối'
                )
            )
            if not self.busy:
                self.status_label.setText('●  ' + text)
            self.status_label.setStyleSheet(
                'color:'
                + (ACCENT if ready else WARNING if alive else MUTED)
                + '; background:'
                + RAISED
                + '; border-radius:9px; padding:4px 10px;'
            )
            self.connect_button.setText('Ngắt kết nối' if wanted else 'Kết nối')
            self.connect_button.setEnabled(not self.busy)
            self.local_button.setEnabled(not self.busy)
            self.local_button.setText('Tắt MCP cục bộ' if status['local_running'] else 'Bật MCP cục bộ')
            self.connection_hint.setText(
                'Sẵn sàng nhận yêu cầu từ ChatGPT Chat.' if ready else 'Private MCP tunnel · không mở cổng công khai'
            )
            self.detail_status.setText(
                'Tunnel: '
                + text
                + '\nMCP cục bộ: '
                + ('đang bật' if status['local_running'] else 'đang tắt')
                + '\nKhóa: lưu trong Windows Credential Manager · không hiển thị'
            )
            setup = self.agent.state / 'sandbox-setup-result.json'
            needs_restart = False
            if setup.exists():
                try:
                    needs_restart = bool(json.loads(setup.read_text('utf-8-sig')).get('RestartNeeded'))
                except (OSError, ValueError):
                    pass
            self.sandbox_label.setText(
                (
                    'Sandbox: chưa sẵn sàng. Lần bật tính năng trước yêu cầu khởi động lại Windows.'
                    if needs_restart
                    else 'Sandbox chưa khả dụng. Chạy mã đang bị khóa.'
                )
                if not status['sandbox']
                else 'Sandbox: thành phần đã cài, chờ kiểm thử chạy mã.'
            )
            sig = (ready, alive)
            if sig != self.connection_signature:
                self.connection_signature = sig
                self.band.flash()
            self.footer_status.setText(
                ('Tunnel sẵn sàng' if ready else 'Chờ kết nối')
                + ' · '
                + ('AI tự sửa theo quyền' if self.agent.source_autonomy_projects() else 'Chưa bật tự sửa')
                + ' · Không chạy host shell'
            )
        except Exception as e:
            self.footer_status.setText('Không tải được trạng thái: ' + str(e))

    def open_url(self, url):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl(url))

    def showEvent(self, event):
        super().showEvent(event)
        if os.name == 'nt':
            try:
                import ctypes

                value = ctypes.c_int(1)
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    int(self.winId()), 20, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                pass

    def closeEvent(self, event):
        if self.busy:
            event.ignore()
            return
        if not self._closing:
            self._closing = True
            self.timer.stop()
            self.controller.close()
        event.accept()


def make_app() -> QApplication:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName('Universal Desktop Bridge')
    app.setOrganizationName('UniversalDesktopBridge')
    app.setStyle(QStyleFactory.create('Fusion'))
    app.setFont(QFont('Segoe UI', 10))
    app.setStyleSheet(STYLE)
    app.setProperty('motion', True)
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor('#345040'))
    app.setPalette(palette)
    return app


def run(home: Path, start_tunnel: bool = False, screenshot: str | Path | None = None) -> int | None:
    if os.name == 'nt':
        import ctypes

        ctypes.windll.kernel32.FreeConsole()  # type: ignore[attr-defined]  # Windows-only API
    app = make_app()
    controller = DesktopController(home)
    settings = controller.settings()
    app.setProperty('motion', settings.get('motion', True))
    lock = QLockFile(str(controller.agent.state / 'manager.lock'))
    lock.setStaleLockTime(30000)
    if not lock.tryLock(50):
        QMessageBox.information(
            None, 'Bridge đang mở', 'Ứng dụng đã được mở. Kiểm tra cửa sổ hoặc thanh tác vụ Windows.'
        )
        return None
    try:
        controller.start_host()
        window = MainWindow(home, controller)
        window.show()
        if start_tunnel or settings.get('autoconnect', False):
            QTimer.singleShot(350, window.toggle_tunnel)
        if screenshot:
            dest = Path(screenshot)

            def capture() -> None:
                if not window.grab().save(str(dest)):
                    raise OSError('GUI screenshot failed')
                app.quit()

            QTimer.singleShot(7000 if start_tunnel else 550, capture)
        return app.exec()
    finally:
        # Always release locally owned resources, even if the UI cannot start.
        controller.close()
        lock.unlock()


def self_test(preview_dir: str | Path | None = None) -> None:
    """Exercise the frozen Qt UI with disposable synthetic data, not the user's state."""
    import tempfile
    from PySide6.QtTest import QTest

    app = make_app()
    app.setProperty('motion', False)
    captures = Path(preview_dir) if preview_dir else None
    if captures:
        captures.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='udb-gui-selftest-') as temp:
        home = Path(temp) / 'bridge'
        source = Path(temp) / 'source'
        source.mkdir()
        (source / 'hello.py').write_text('value = 1\n', encoding='utf-8')
        ctl = DesktopController(home, monitor=False)
        ctl.agent.register_local('demo', str(source), True)
        request = ctl.agent.propose_write('demo', 'gui-proof.py', 'print("<b>literal source</b>")\n', 'NEW')
        win = MainWindow(home, ctl)
        win.show()
        app.processEvents()
        assert win.projects.topLevelItemCount() == 1
        assert win.selected_project['id'] == 'demo'
        assert win.review_button.isEnabled()
        assert win.pending_rows[request['request_id']]['status'] == 'pending'
        QTest.mouseClick(win.nav_buttons[1], Qt.MouseButton.LeftButton)
        app.processEvents()
        assert win.stack.currentIndex() == 1 and win.requests.topLevelItemCount() == 1
        dialog = ApprovalDialog(ctl.agent, win.pending_rows[request['request_id']], win)
        dialog.show()
        app.processEvents()
        assert not dialog.approve.isEnabled()
        assert dialog.full.toPlainText() == 'print("<b>literal source</b>")\n'
        if captures:
            assert dialog.grab().save(str(captures / 'approval-dialog.png'))
        dialog.reviewed.setFocus()
        QTest.keyClick(dialog.reviewed, Qt.Key.Key_Space)
        app.processEvents()
        assert dialog.reviewed.isChecked(), 'Review checkbox keyboard activation failed'
        assert dialog.approve.isEnabled()
        # Intentionally do not click Approve. A checked box must not write anything.
        assert not (source / 'gui-proof.py').exists()
        assert ctl.agent.request_status(request['request_id'])['status'] == 'pending'
        dialog.reject()
        app.processEvents()
        browse = SourceDialog(ctl.agent, 'demo', win)
        found = False
        for i in range(browse.files.count()):
            item = browse.files.item(i)
            entry = item.data(Qt.ItemDataRole.UserRole)
            if entry.get('path') == 'hello.py':
                browse.open_item(item)
                found = True
                break
        assert found and browse.code.toPlainText() == 'value = 1\n'
        browse.accept()
        from . import desktop_vm

        original_available = desktop_vm.available
        try:
            desktop_vm.available = lambda: True
            proposal = ctl.agent.propose_desktop('demo', 'Synthetic GUI permission preview only', 600)
        finally:
            desktop_vm.available = original_available
        win.refresh()
        permission = ApprovalDialog(ctl.agent, win.pending_rows[proposal['request_id']], win)
        permission.show()
        app.processEvents()
        assert permission.is_desktop and not permission.approve.isEnabled()
        assert 'Sandbox guest only' in permission.preview.toPlainText()
        if captures:
            assert permission.grab().save(str(captures / 'desktop-permission.png'))
        permission.reject()
        app.processEvents()
        assert ctl.agent.request_status(proposal['request_id'])['status'] == 'pending'
        # Synthetic history exercises the observer tabs without launching a VM.
        jid = 'a' * 32
        job = ctl.agent.state / 'jobs' / jid
        (job / 'input').mkdir(parents=True)
        (job / 'output').mkdir()
        script = 'print("<b>visible observer proof</b>")\n'
        (job / 'input' / 'task.py').write_text(script, encoding='utf-8')
        (job / 'output' / 'output.log').write_text(
            'old line\n' * 5000 + 'live <b>literal output</b>\n', encoding='utf-8'
        )
        with ctl.agent.connect() as c:
            c.execute(
                'INSERT INTO jobs VALUES (?,?,?)',
                (jid, 'reviewing', json.dumps({'project': 'demo', 'language': 'python', 'review_seconds': 60})),
            )
        win.refresh()
        win.jobs.setCurrentItem(win.jobs.topLevelItem(0))
        win.show_job()
        assert win.job_script.toPlainText() == script
        assert 'live <b>literal output</b>' in win.job_output.toPlainText()
        # Exercise real layout and painting, including a first-time page switch,
        # compact desktop, and both motion settings. Never approve a request.
        for motion in (False, True):
            app.setProperty('motion', motion)
            for width, height in ((1220, 820), (960, 680)):
                win.resize(width, height)
                app.processEvents()
                for i in range(6):
                    QTest.mouseClick(win.nav_buttons[i], Qt.MouseButton.LeftButton)
                    QTest.qWait(220)
                    assert win.stack.currentIndex() == i
                    page = win.stack.currentWidget()
                    header = page.findChild(QWidget, 'PageHeader')
                    title = header.findChild(QLabel, 'H1')
                    subtitle = header.findChild(QLabel, 'Subtitle')
                    assert title.isVisible() and subtitle.isVisible()
                    assert title.height() >= title.fontMetrics().height()
                    assert title.geometry().bottom() < subtitle.geometry().top(), 'Header labels overlap'
                    assert header.width() <= page.width() and page.width() > 600
                    assert page.graphicsEffect() is None and win.stack.graphicsEffect() is None
                    assert win.nav_buttons[i].isVisible()
                    if i == 0:
                        assert win.project_path.isReadOnly()
                        assert win.project_path.text() == str(source)
                        assert win.project_path.parentWidget().rect().contains(win.project_path.geometry()), (
                            'Project path overflows its card'
                        )
                    if captures and not motion:
                        assert win.grab().save(str(captures / f'page-{i}-{width}.png'))
        # Empty states hide their corresponding tables and disable review.
        # Expire only the synthetic request in this disposable test database.
        with ctl.agent.connect() as c:
            c.execute('UPDATE requests SET created=0')
        win.refresh()
        win.navigate(1)
        QTest.qWait(100)
        assert win.approval_empty.isVisible() and win.requests.isHidden()
        assert not win.approval_review.isEnabled()
        if captures:
            assert win.grab().save(str(captures / 'approvals-empty.png'))
        assert not (source / 'gui-proof.py').exists()
        win.close()
        app.processEvents()
        ctl.close()
