"""Shared visual theme and reusable Qt widgets for the UDB manager."""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QByteArray,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRectF,
    QRunnable,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QSyntaxHighlighter,
    QTextCharFormat,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .ui_identity import ICON_SVG

BG = "#0d1216"
PANEL = "#151e24"
RAISED = "#1d2b32"
LINE = "#2b3c45"
TEXT = "#edf5f3"
MUTED = "#9fb2ba"
ACCENT = "#78dfbc"
WARNING = "#edc486"
DANGER = "#f4a6a6"


STYLE = f"""
QWidget {{ color:{TEXT}; font-family:"Segoe UI"; font-size:14px; }}
QMainWindow, QDialog {{ background:{BG}; }}
QWidget#Root, QWidget#Page, QWidget#PageHeader, QStackedWidget {{ background:{BG}; }}
QFrame#Sidebar {{ background:{PANEL}; border-right:1px solid {LINE}; }}
QFrame#Panel {{ background:{PANEL}; border:1px solid {LINE}; border-radius:16px; }}
QFrame#Hero {{ background:#152c29; border:1px solid #2d5447; border-radius:16px; }}
QFrame#Footer {{ border-top:1px solid {LINE}; background:{BG}; }}
QLabel {{ background:transparent; border:0; }}
QLabel#H1 {{ font-size:27px; font-weight:600; }}
QLabel#H2 {{ font-size:18px; font-weight:600; }}
QLabel#EmptyTitle {{ font-size:21px; font-weight:600; }}
QLabel#EmptyIcon {{ color:{ACCENT}; font-size:32px; }}
QLabel#Subtitle {{ color:{MUTED}; font-size:14px; }}
QLabel#Brand {{ font-size:30px; font-weight:600; letter-spacing:-1px; }}
QLabel#Eyebrow {{ color:{MUTED}; font-size:12px; letter-spacing:1.2px; }}
QLabel#Muted {{ color:{MUTED}; font-size:14px; }}
QLabel#Fine {{ color:{MUTED}; font-size:13px; }}
QLabel#Warning {{ color:{WARNING}; font-size:12px; }}
QLabel#Badge {{ background:{RAISED}; color:{ACCENT}; border-radius:9px; padding:3px 8px; }}
QLineEdit, QPlainTextEdit, QListWidget, QTreeWidget {{ background:{BG}; color:{TEXT}; border:1px solid {LINE}; border-radius:8px; selection-background-color:#28483f; selection-color:{TEXT}; }}
QLineEdit {{ padding:10px 12px; }}
QLineEdit#ProjectPath {{ background:transparent; border:0; padding:0; color:{MUTED}; font-size:12px; }}
QLineEdit:focus, QPlainTextEdit:focus, QTreeWidget:focus, QListWidget:focus {{ border-color:#78dfbc; }}
QPlainTextEdit {{ padding:12px; font-family:"Cascadia Code",Consolas,monospace; font-size:13px; }}
QTreeWidget {{ border:0; outline:0; background:transparent; alternate-background-color:{PANEL}; }}
QTreeWidget::item {{ padding:12px 10px; border-bottom:1px solid {LINE}; }}
QTreeWidget::item:selected {{ background:#213b35; color:{TEXT}; }}
QTreeWidget::item:hover {{ background:#1a2b32; }}
QHeaderView::section {{ background:{BG}; color:{MUTED}; border:0; border-bottom:1px solid {LINE}; padding:10px 7px; font-size:12px; }}
QListWidget::item {{ padding:10px; }}
QListWidget::item:selected {{ background:#213b35; }}
QScrollArea {{ background:transparent; border:0; }}
QScrollBar:vertical {{ background:{BG}; width:10px; margin:0; }}
QScrollBar::handle:vertical {{ background:#3a515b; min-height:28px; border-radius:5px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height:0; }}
QScrollBar:horizontal {{ background:{BG}; height:10px; }}
QScrollBar::handle:horizontal {{ background:#3a515b; min-width:28px; border-radius:5px; }}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width:0; }}
QTabWidget::pane {{ border:1px solid {LINE}; border-radius:7px; }}
QTabBar::tab {{ background:{PANEL}; color:{MUTED}; padding:10px 16px; border-bottom:2px solid transparent; }}
QTabBar::tab:selected {{ color:{TEXT}; border-bottom:2px solid {ACCENT}; }}
QCheckBox {{ spacing:10px; padding:7px 0; }}
QCheckBox::indicator {{ width:17px; height:17px; background:{BG}; border:1px solid #536b75; border-radius:4px; }}
QCheckBox::indicator:checked {{ background:{ACCENT}; border:4px solid #254f41; }}
QToolButton {{ color:{MUTED}; background:transparent; border:0; padding:8px 0; text-align:left; }}
QToolButton:hover {{ color:{TEXT}; }}
QToolButton:focus {{ border:1px solid {ACCENT}; border-radius:6px; }}
QToolTip {{ background:{RAISED}; color:{TEXT}; border:1px solid {LINE}; padding:7px; }}
QMessageBox {{ background:{BG}; }}
QMessageBox QPushButton, QInputDialog QPushButton {{ background:{RAISED}; border:1px solid {LINE}; border-radius:6px; padding:8px 18px; min-width:75px; }}
QMessageBox QPushButton:hover, QInputDialog QPushButton:hover {{ background:#314438; }}
QSplitter::handle {{ background:transparent; width:1px; }}
"""


def label(text="", name="", wrap=False):
    w = QLabel(text)
    w.setTextFormat(Qt.TextFormat.PlainText)
    if name:
        w.setObjectName(name)
    w.setWordWrap(wrap)
    w.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
    w.setMinimumHeight(22)
    return w


def row(*widgets):
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    for w in widgets:
        if w is None:
            layout.addStretch()
        else:
            layout.addWidget(w)
    return layout


def panel():
    w = QFrame()
    w.setObjectName("Panel")
    return w


def empty_state(title, description, glyph="check"):
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
    heading = label(title, "EmptyTitle", True)
    heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lay.addWidget(heading)
    body = label(description, "Muted", True)
    body.setAlignment(Qt.AlignmentFlag.AlignCenter)
    body.setMinimumHeight(64)
    lay.addWidget(body)
    lay.addStretch()
    return card


def icon(name, color=TEXT, size=20):
    pm = QPixmap(size * 2, size * 2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size * 2 / 24, size * 2 / 24)
    p.setPen(
        QPen(
            QColor(color),
            1.6,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
    )
    if name == "brand":
        QSvgRenderer(QByteArray(ICON_SVG.encode("utf-8"))).render(
            p, QRectF(0, 0, 24, 24)
        )
    elif name in ("folder", "projects"):
        path = QPainterPath()
        path.moveTo(3, 6)
        path.lineTo(9, 6)
        path.lineTo(11, 9)
        path.lineTo(21, 9)
        path.lineTo(21, 20)
        path.lineTo(3, 20)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "shield":
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
    elif name == "browser":
        p.drawRoundedRect(QRectF(3, 4, 18, 16), 3, 3)
        p.drawLine(3, 9, 21, 9)
        p.drawPoint(6, 6)
        p.drawPoint(9, 6)
    elif name == "pause":
        p.drawLine(9, 5, 9, 19)
        p.drawLine(15, 5, 15, 19)
    elif name == "play":
        path = QPainterPath()
        path.moveTo(8, 5)
        path.lineTo(19, 12)
        path.lineTo(8, 19)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "cursor":
        path = QPainterPath()
        path.moveTo(5, 3)
        path.lineTo(6, 20)
        path.lineTo(11, 15)
        path.lineTo(19, 15)
        path.closeSubpath()
        p.drawPath(path)
    elif name == "power":
        p.drawArc(QRectF(4, 4, 16, 16), 40 * 16, 280 * 16)
        p.drawLine(12, 2, 12, 11)
    elif name == "plus":
        p.drawLine(12, 5, 12, 19)
        p.drawLine(5, 12, 19, 12)
    elif name == "arrow":
        p.drawLine(5, 12, 19, 12)
        p.drawLine(14, 7, 19, 12)
        p.drawLine(14, 17, 19, 12)
    elif name == "activity":
        path = QPainterPath()
        path.moveTo(2, 12)
        path.lineTo(7, 12)
        path.lineTo(10, 5)
        path.lineTo(14, 19)
        path.lineTo(17, 12)
        path.lineTo(22, 12)
        p.drawPath(path)
    elif name == "review":
        p.drawEllipse(QRectF(4, 3, 5, 5))
        p.drawLine(7, 8, 7, 20)
        p.drawLine(7, 12, 17, 12)
        p.drawLine(17, 12, 17, 20)
        p.drawEllipse(QRectF(14.5, 3, 5, 5))
        p.drawLine(17, 8, 17, 12)
    elif name == "settings":
        for y in (6, 12, 18):
            p.drawLine(3, y, 21, y)
        for x, y in ((8, 6), (16, 12), (10, 18)):
            p.fillRect(QRectF(x - 2, y - 2, 4, 4), QColor(PANEL))
            p.drawEllipse(QRectF(x - 2, y - 2, 4, 4))
    elif name == "file":
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
    elif name == "external":
        p.drawLine(13, 4, 20, 4)
        p.drawLine(20, 4, 20, 11)
        p.drawLine(20, 4, 10, 14)
        p.drawLine(10, 5, 4, 5)
        p.drawLine(4, 5, 4, 20)
        p.drawLine(4, 20, 19, 20)
        p.drawLine(19, 20, 19, 14)
    elif name == "check":
        p.drawLine(5, 12, 10, 17)
        p.drawLine(10, 17, 20, 6)
    else:
        p.drawRoundedRect(QRectF(4, 5, 16, 14), 3, 3)
        p.drawLine(8, 10, 16, 10)
        p.drawLine(8, 14, 14, 14)
    p.end()
    pm.setDevicePixelRatio(2)
    return QIcon(pm)


class ResponsiveRow(QWidget):
    """Use viewport width, not a card's minimum width, to choose the layout."""

    def __init__(self, *widgets):
        super().__init__()
        self.viewport = None
        self.box = QBoxLayout(QBoxLayout.Direction.TopToBottom, self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.box.setSpacing(14)
        for index, widget in enumerate(widgets):
            self.box.addWidget(widget, 3 if index == 0 else 2)

    def _arrange(self, width):
        direction = (
            QBoxLayout.Direction.LeftToRight
            if width >= 850
            else QBoxLayout.Direction.TopToBottom
        )
        if self.box.direction() != direction:
            self.box.setDirection(direction)
            self.box.activate()
            self.updateGeometry()

    def showEvent(self, event):
        parent = self.parentWidget()
        while parent is not None and not isinstance(parent, QScrollArea):
            parent = parent.parentWidget()
        if parent is not None:
            self.viewport = parent.viewport()
            self.viewport.installEventFilter(self)
            self._arrange(self.viewport.width())
        super().showEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.viewport and event.type() == QEvent.Type.Resize:
            self._arrange(event.size().width())
        return super().eventFilter(watched, event)

    def resizeEvent(self, event):
        self._arrange(
            self.viewport.width() if self.viewport is not None else self.width()
        )
        super().resizeEvent(event)


class StatusPill(QLabel):
    """Compact, accessible state indicator; avoid implying activity from a connection."""

    def __init__(self, text="", tone="neutral"):
        super().__init__()
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(28)
        self.set_status(text, tone)

    def set_status(self, text, tone="neutral"):
        if getattr(self, "_status", None) == (text, tone):
            return
        self._status = (text, tone)
        colors = {
            "ready": ("#1b3c31", ACCENT),
            "paused": ("#3b3021", WARNING),
            "owner": ("#20384a", "#a8d4ee"),
            "neutral": (RAISED, MUTED),
        }
        background, foreground = colors.get(tone, colors["neutral"])
        self.setText("●  " + text)
        self.setAccessibleName(text)
        self.setStyleSheet(
            f"background:{background};color:{foreground};border:0;border-radius:13px;"
            "padding:5px 11px;font-size:12px;font-weight:600;"
        )


class MotionButton(QPushButton):
    def __init__(self, text="", glyph="", kind="quiet", parent=None):
        super().__init__(text, parent)
        self.glyph = glyph
        self.kind = kind
        self._hover = 0.0
        self._selected = False
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(42)
        self.setAccessibleName(text)
        self.anim = QPropertyAnimation(self, b"hover", self)
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
        if not QApplication.instance().property("motion"):
            self.set_hover(v)
            return
        self.anim.stop()
        self.anim.setStartValue(self._hover)
        self.anim.setEndValue(v)
        self.anim.start()

    def sizeHint(self):
        return QSize(
            max(
                88,
                self.fontMetrics().horizontalAdvance(self.text())
                + 44
                + (24 if self.glyph else 0),
            ),
            42,
        )

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        base = QColor(
            ACCENT
            if self.kind == "primary"
            else ("#213b35" if self._selected else PANEL)
        )
        over = QColor("#9ae9cf" if self.kind == "primary" else "#233941")
        bg = QColor(
            *[
                round(
                    base.getRgb()[i] * (1 - self._hover)
                    + over.getRgb()[i] * self._hover
                )
                for i in range(3)
            ]
        )
        fg = QColor(
            "#0c3025"
            if self.kind == "primary"
            else (DANGER if self.kind == "danger" else TEXT)
        )
        if not self.isEnabled():
            bg = QColor("#202b32")
            fg = QColor("#73868e")
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(bg)
        p.setPen(
            QPen(
                QColor(
                    ACCENT
                    if self.hasFocus()
                    else (LINE if self.kind != "primary" else ACCENT)
                ),
                1,
            )
        )
        if self.kind == "nav" and not self.hasFocus():
            p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 11, 11)
        offset = 1 if self.isDown() else 0
        width = self.fontMetrics().horizontalAdvance(self.text()) + (
            25 if self.glyph else 0
        )
        x = 16 if self.kind == "nav" else (self.width() - width) / 2
        if self.glyph:
            icon(self.glyph, fg.name()).paint(
                p, int(x), int((self.height() - 18) / 2) + offset, 18, 18
            )
            x += 26
        p.setPen(fg)
        p.setFont(self.font())
        p.drawText(
            QRectF(x, offset, self.width() - x - 9, self.height()),
            Qt.AlignmentFlag.AlignVCenter,
            self.text(),
        )
        p.end()


class StatusBand(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("Panel")
        self._progress = 1.0
        self.anim = QPropertyAnimation(self, b"progress", self)
        self.anim.setDuration(1050)
        self.anim.setEasingCurve(QEasingCurve.Type.InOutCubic)

    def get_progress(self):
        return self._progress

    def set_progress(self, v):
        self._progress = v
        self.update()

    progress = Property(float, get_progress, set_progress)

    def flash(self):
        if not QApplication.instance().property("motion"):
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
            g.setColorAt(0, QColor("#0078dfbc"))
            g.setColorAt(0.5, QColor(ACCENT))
            g.setColorAt(1, QColor("#0078dfbc"))
            p.fillRect(QRectF(x, self.height() - 2, 220, 2), g)
            p.end()


class DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text):
        if text.startswith("+") and not text.startswith("+++"):
            color = ACCENT
            background = "#193b31"
        elif text.startswith("-") and not text.startswith("---"):
            color = DANGER
            background = "#3d252b"
        elif text.startswith("@@"):
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
