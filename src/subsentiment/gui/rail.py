"""The navigation rail down the window's left edge, and the segmented switch the header's range presets use.

The rail has one button per page (Overview, Timeline, Words, Posts, Comments) and two that open a drawer
beside it instead of a page: Fetch at the top and Saved at the bottom. Icons are drawn here, in the theme's
colours, so they follow light and dark without image files.
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QAbstractButton, QButtonGroup, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout

from subsentiment.gui import theme
from subsentiment.gui.theme import mark
from subsentiment.gui.widgets import label

PAGES = {"overview": "Overview", "timeline": "Timeline", "words": "Words", "posts": "Posts", "comments": "Comments"}
DRAWERS = {"fetch": "Fetch", "saved": "Saved"}


class RailButton(QAbstractButton):
    """An icon over a short name. Checked, both take the accent on a tinted rounded square; ``busy`` adds a
    dot to the icon (a fetch running while its drawer is closed)."""

    def __init__(self, key: str, text: str):
        super().__init__()
        self.key = key
        self.busy = False
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(text)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover)

    def _caption_font(self) -> QFont:
        f = QFont(self.font())
        f.setPointSizeF(f.pointSizeF() * 0.8)
        f.setWeight(QFont.Weight.Medium)
        return theme.smooth_text(f)

    def sizeHint(self) -> QSize:
        m = QFontMetrics(self._caption_font())
        return QSize(max(60, m.horizontalAdvance(self.text()) + 10), 34 + m.height() + 6)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.update()

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = theme.tokens()
        r = QRectF(self.rect())
        on = self.isChecked()
        if on or self.underMouse():
            fill = QColor(t["accent"] if on else t["text"])
            fill.setAlpha(28 if on else 14)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            p.drawRoundedRect(r.adjusted(2, 1, -2, -1), 12, 12)
        color = QColor(t["accent"] if on else t["muted"])
        pen = QPen(color, 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        c = QPointF(r.center().x(), 19)
        self._draw_icon(p, c)
        if self.busy:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t["accent"]))
            p.drawEllipse(c + QPointF(9, -8), 3.5, 3.5)
        p.setPen(color)
        p.setFont(self._caption_font())
        p.drawText(QRectF(0, 33, r.width(), r.height() - 35), Qt.AlignmentFlag.AlignHCenter, self.text())

    def _draw_icon(self, p: QPainter, c: QPointF) -> None:
        k = self.key
        if k == "fetch":
            p.drawEllipse(c, 8, 8)
            p.drawLine(c + QPointF(0, -4), c + QPointF(0, 4))
            p.drawLine(c + QPointF(-4, 0), c + QPointF(4, 0))
        elif k == "overview":
            for dx in (-6, 2):
                for dy in (-6, 2):
                    p.drawRoundedRect(QRectF(c.x() + dx, c.y() + dy, 5, 5), 1.2, 1.2)
        elif k == "timeline":
            path = QPainterPath(c + QPointF(-8, 3))
            for x, y in ((-4, -2), (0, 1), (4, -5), (8, -2)):
                path.lineTo(c + QPointF(x, y))
            p.drawPath(path)
            p.drawLine(c + QPointF(-8, 7), c + QPointF(8, 7))
        elif k == "words":
            f = QFont(theme.DISPLAY_FAMILY)
            f.setPixelSize(15)
            f.setWeight(QFont.Weight.Bold)
            p.setFont(theme.smooth_text(f))
            p.drawText(QRectF(c.x() - 14, c.y() - 10, 28, 20), Qt.AlignmentFlag.AlignCenter, "Aa")
        elif k == "posts":
            for i, width in enumerate((16, 16, 10)):
                y = c.y() - 5 + i * 5
                p.drawLine(QPointF(c.x() - 8, y), QPointF(c.x() - 8 + width, y))
        elif k == "comments":
            p.drawRoundedRect(QRectF(c.x() - 8, c.y() - 7, 16, 11), 3, 3)
            p.drawLine(c + QPointF(-3, 4), c + QPointF(-5, 8))
        elif k == "saved":
            p.drawEllipse(QRectF(c.x() - 7, c.y() - 8, 14, 5))
            p.drawLine(c + QPointF(-7, -5.5), c + QPointF(-7, 6))
            p.drawLine(c + QPointF(7, -5.5), c + QPointF(7, 6))
            p.drawArc(QRectF(c.x() - 7, c.y() + 3.5, 14, 5), 180 * 16, 180 * 16)


class NavRail(QFrame):
    """``page_chosen`` sends a page key when its button is clicked; ``drawer_toggled`` sends a drawer key
    when one is opened and "" when the open one is closed by clicking it again."""

    page_chosen = Signal(str)
    drawer_toggled = Signal(str)

    def __init__(self):
        super().__init__()
        mark(self, "glass", "true")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 14, 6, 8)
        lay.setSpacing(4)
        logo = label("ikari", "title")
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(logo)
        lay.addSpacing(10)
        self.buttons: dict[str, RailButton] = {}
        self._pages = QButtonGroup(self)
        for key, text in [("fetch", DRAWERS["fetch"]), *PAGES.items()]:
            lay.addWidget(self._add(key, text))
        lay.addStretch(1)
        lay.addWidget(self._add("saved", DRAWERS["saved"]))
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)  # as wide as its widest button

    def _add(self, key: str, text: str) -> RailButton:
        b = self.buttons[key] = RailButton(key, text)
        if key in PAGES:
            self._pages.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self.page_chosen.emit(k))
        else:
            b.clicked.connect(lambda on, k=key: self._drawer_clicked(k, on))
        return b

    def _drawer_clicked(self, key: str, on: bool) -> None:
        self.set_drawer(key if on else "")
        self.drawer_toggled.emit(key if on else "")

    def set_page(self, key: str) -> None:
        self.buttons[key].setChecked(True)

    def page(self) -> str:
        return next(k for k in PAGES if self.buttons[k].isChecked())

    def set_drawer(self, key: str) -> None:
        """Mark which drawer is open ("" for none), without sending anything."""
        for k in DRAWERS:
            self.buttons[k].setChecked(k == key)


class Segmented(QFrame):
    """A row of mutually exclusive choices drawn as one control (the header's 1W / 1M / 3M / All). Clicking
    one sends ``chosen`` with its key; ``set_current`` marks one without sending, or none with ""."""

    chosen = Signal(str)

    def __init__(self, choices: dict[str, str]):
        super().__init__()
        mark(self, "segmented", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        self.group.setExclusive(False)  # so that none can be marked, when the chosen days match no preset
        self.buttons: dict[str, QPushButton] = {}
        for key, text in choices.items():
            b = self.buttons[key] = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: (self.set_current(k), self.chosen.emit(k)))
            self.group.addButton(b)
            lay.addWidget(b)

    def set_current(self, key: str) -> None:
        for k, b in self.buttons.items():
            b.setChecked(k == key)

    def current(self) -> str:
        return next((k for k, b in self.buttons.items() if b.isChecked()), "")
