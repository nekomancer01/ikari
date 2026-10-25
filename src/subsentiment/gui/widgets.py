"""Reusable widgets and small layout helpers shared by the main window and the Download coverage window.

Nothing here knows about ikari's data; the windows put these together.
"""
from __future__ import annotations

import math
import time
from typing import Callable

from PySide6.QtCore import QDate, QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QAbstractScrollArea, QCalendarWidget, QComboBox, QDateEdit, QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QScrollArea, QSpinBox, QTableView, QToolButton, QVBoxLayout, QWidget,
)

from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
from subsentiment.gui.days import WEEKDAYS
from subsentiment.gui.theme import mark


def fit_to_screen(size: QSize, screen) -> QSize:
    """``size``, shrunk to fit the screen's free area (a 1920x1200 screen at 150% has about 1280x750)."""
    if screen is None:
        return size
    free = screen.availableGeometry()
    return QSize(min(size.width(), int(free.width() * 0.94)), min(size.height(), int(free.height() * 0.92)))


def plural(n: int, noun: str) -> str:
    return f"{n:,}\u00a0{noun}{'' if n == 1 else 's'}"  # no-break space keeps a number with its noun


def panel(kind: str = "true", margins: int = 18, spacing: int = 12) -> tuple[QFrame, QVBoxLayout]:
    """A rounded, outlined panel: kind "true" is a page-level panel, "tile" and "solid" a card on one."""
    frame = mark(QFrame(), "glass", kind)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    return frame, lay


def label(text: str, role: str) -> QLabel:
    return mark(QLabel(text), "role", role)


def section(text: str) -> QLabel:
    return label(text, "section")


class ElidedLabel(QLabel):
    """One line that ends in "…" when there isn't room (the tooltip has it all), instead of wrapping.

    A wrapped label in the main window's layout gets no extra height for its second line, so it was
    cut off on narrow windows; and an unwrapped one would hold the window wider than small screens."""

    def __init__(self, text: str = "", role: str | None = None):
        super().__init__()
        if role:
            self.setProperty("role", role)
        self._full = ""
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 2, super().sizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()

    def _elide(self) -> None:
        m = self.contentsMargins()
        room = max(self.width() - m.left() - m.right(), 1)
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, room)
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class StatusLabel(ElidedLabel):
    """The muted line under Fetch: one line that ends in "…" (the tooltip has it all), no room while empty.

    It used to wrap, and a long "Added … Updated …" message took three lines, which pushed the sidebar
    into scrolling and shifted its fields."""

    def __init__(self):
        super().__init__("", "muted")
        self.setVisible(False)

    def setText(self, text: str) -> None:
        appearing = bool(text) and not self.isVisible()
        super().setText(text)
        self.setVisible(bool(text))
        if appearing:
            motion.fade_in(self)

    def text(self) -> str:
        return self.full_text()


class ProgressBar(QProgressBar):
    """A progress bar that is redrawn with the next frame. Qt's paints the whole window there and then on every
    new value, which a fetch sends for each comment thread it saves."""

    def setValue(self, value: int) -> None:
        self.setUpdatesEnabled(False)  # repaint() does nothing then; turning them back on asks for a redraw
        super().setValue(value)
        self.setUpdatesEnabled(True)

    def setRange(self, low: int, high: int) -> None:
        self.setUpdatesEnabled(False)
        super().setRange(low, high)
        self.setUpdatesEnabled(True)


class VerticalScrollArea(QScrollArea):
    """Scrolls up and down only, and keeps the window at least as wide as its contents need."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def minimumSizeHint(self) -> QSize:
        inner = self.widget().minimumSizeHint() if self.widget() else QSize(0, 0)
        return QSize(inner.width() + self.verticalScrollBar().sizeHint().width(), 200)


class ShrinkingComboBox(QComboBox):
    """Full width when there's room; on a narrow window it gives up width (its text ends in "…")."""

    def minimumSizeHint(self) -> QSize:
        full = super().minimumSizeHint()
        return QSize(min(full.width(), 120), full.height())


class CommentCap(QObject):
    """How many comments to keep on each post, ``count`` (0 shows as "All"), and which ones, ``pick``: the
    highest-scored or a random sample that stays the same for each post; or, ignoring the count, "sample": a
    random sample of each day's comments big enough to measure its sentiment (collect.Sample). The Fetch drawer
    and the Coverage page each place one, remembered under their own QSettings prefix as "comments_per_post" and
    "comment_pick"."""

    PICKS = {"top": "Top scoring", "random": "Random", "sample": "Sample"}

    def __init__(self, settings, prefix: str, default: int, most: int, parent=None):
        super().__init__(parent)
        self.settings, self.prefix, self.default = settings, prefix, default
        self._enabled = True
        self.count = QSpinBox()
        self.count.setRange(0, most)
        self.count.setSingleStep(10)
        self.count.setSuffix(" per post")
        # The stylesheet's horizontal padding doesn't reach a spin box's inner editor; match the other inputs.
        self.count.findChild(QLineEdit).setTextMargins(8, 0, 0, 0)
        self.count.setSpecialValueText("All")
        self.count.setToolTip("The most comments kept on each post. Go below 1 for All: every comment on each post.")
        self.count.setMinimumWidth(100)  # its size hint would squeeze a form's labels
        self.pick = QComboBox()
        for key, text in self.PICKS.items():
            self.pick.addItem(text, key)
        self.pick.setToolTip("Which comments a post keeps when it has more than that: the highest-scored, or a "
                             "random sample that stays the same each time the post is asked about.\n"
                             "Sample: enough of each day's comments, picked at random across its posts by how many "
                             "each has, to measure the day's sentiment within ±2 points (99% confidence): 4,148 "
                             "of a big day, fewer of a smaller one; a day of up to 4,148 comments is downloaded whole.")
        for signal in (self.count.valueChanged, self.pick.currentIndexChanged):
            signal.connect(lambda _: self.set_enabled(self._enabled))
        self.restore()

    def request(self) -> dict:
        """The FetchRequest fields this sets."""
        return {"comments_per_post": self.count.value(), "comment_pick": self.pick.currentData()}

    @property
    def sampling(self) -> bool:
        return self.pick.currentData() == "sample"

    def set_enabled(self, on: bool) -> None:
        """Both off with ``on`` False. The count is off while sampling, which sets its own; under All, Top scoring
        and Random are, as nothing is left out to choose between, leaving Sample to take (and, from Sample, the
        way back)."""
        self._enabled = on
        self.count.setEnabled(on and not self.sampling)
        either = self.count.value() > 0 or self.sampling
        for key in ("top", "random"):
            self.pick.model().item(self.pick.findData(key)).setEnabled(either)
        self.pick.setEnabled(on)

    def restore(self) -> None:
        self.count.setValue(self.settings.value(f"{self.prefix}/comments_per_post", self.default, int))
        pick = self.settings.value(f"{self.prefix}/comment_pick", "top", str)
        self.pick.setCurrentIndex(max(self.pick.findData(pick), 0))

    def save(self) -> None:
        self.settings.setValue(f"{self.prefix}/comments_per_post", self.count.value())
        self.settings.setValue(f"{self.prefix}/comment_pick", self.pick.currentData())

class Choice(QFrame):
    """A row of words in a soft track, one of them chosen (a raised pill): Download coverage's Posts / Comments /
    Both and its list filters. ``options`` is [(key, text)]; ``changed`` sends the chosen key."""

    changed = Signal(str)

    def __init__(self, options: list[tuple[str, str]], current: str, small: bool = False):
        super().__init__()
        mark(self, "choice", True)
        if small:
            mark(self, "small", True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.buttons: dict[str, QPushButton] = {}
        for key, text in options:
            b = mark(QPushButton(text), "flat", True)
            b.setCheckable(True)
            b.setAutoExclusive(True)
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.changed.emit(k))
            self.buttons[key] = b
            lay.addWidget(b)
        self.set_value(current if current in self.buttons else options[0][0])

    def value(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())

    def set_value(self, key: str) -> None:
        self.buttons[key].setChecked(True)


class QuietScrollBars(QObject):
    """Shows a list's scroll bar handle only while the pointer is over the list, like macOS overlay bars.

    The bar keeps its place, so text doesn't shift when it appears."""

    def __init__(self, area: QAbstractScrollArea):
        super().__init__(area)
        self._bar = area.verticalScrollBar()
        self._bar.setProperty("quiet", True)
        self._show(False)
        area.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            self._show(event.type() == QEvent.Type.Enter)
        return False

    def _show(self, on: bool) -> None:
        self._bar.setProperty("hot", on)
        self._bar.style().unpolish(self._bar)
        self._bar.style().polish(self._bar)
        self._bar.update()


class ShareBar(QWidget):
    """Thin rounded bar showing a share from 0 to 1 (None draws the empty track).

    Filled in the palette colour ``color_key`` when one is given, else in the accent; a complete
    bar is green. While ``waiting`` (for the number it will show) a short stretch of accent sweeps across
    it instead; with motion off the whole track is tinted."""

    def __init__(self, color_key: str | None = None):
        super().__init__()
        self.color_key = color_key
        self.share: float | None = None
        self.complete = False
        self.waiting = False
        self._drawn = motion.Tween(lambda _: self.update(), self)  # the share on screen, easing to self.share
        self.setFixedHeight(6)

    def set(self, share: float | None, complete: bool = False, animate: bool = False,
            waiting: bool = False) -> None:
        """With animate, the bar grows or shrinks to its new length instead of jumping there."""
        self.share = None if share is None else max(0.0, min(1.0, share))
        self.complete = complete
        self.waiting = waiting
        self._drawn.set(self.share or 0.0, animate)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        t = theme.tokens()
        p.setBrush(QColor(t["soft"]))
        p.drawRoundedRect(r, 3, 3)
        if self.waiting:
            accent = QColor(t["accent"])
            phase = motion.throb(self)
            if phase is None:
                accent.setAlpha(90)
                p.setBrush(accent)
                p.drawRoundedRect(r, 3, 3)
                return
            # A third of the track wide, in from the left edge and out at the right, easing in and out.
            span = r.width() / 3
            x = r.x() - span + (r.width() + span) * (0.5 - math.cos(math.pi * phase) / 2)
            p.setClipRect(r)
            p.setBrush(accent)
            p.drawRoundedRect(QRectF(x, r.y(), span, r.height()), 3, 3)
            return
        share = self._drawn.value
        if not share:
            return
        fill = QRectF(r.x(), r.y(), max(r.height(), r.width() * share), r.height())
        if self.complete or self.color_key:
            p.setBrush(QColor(palette()["positive" if self.complete else self.color_key]))
        else:
            p.setBrush(QColor(t["accent"]))
        p.drawRoundedRect(fill, 3, 3)


class Throbber(ShareBar):
    """A ShareBar's sweep laid over ``line`` (a divider), taking no room, once the window has been waiting on
    something for DELAY seconds: ``waiting()`` names what (a caption shown at the line's right end, in
    ``beside``'s row when its text leaves room), "" for a wait to show without one, or None for none. It's asked
    a few times a second, so a wait needn't say when it starts or ends, and one over within the delay shows
    nothing at all."""

    DELAY = 1.0  # seconds a wait goes on before it shows
    POLL_MS = 100

    def __init__(self, line: QWidget, waiting: Callable[[], str | None], beside: ElidedLabel | None = None,
                 inset: int = 16):
        super().__init__()
        host = line.parentWidget()
        self.setParent(host)
        self.line, self.beside, self.inset, self._waiting = line, beside, inset, waiting
        self.setFixedHeight(3)
        self.set(None, waiting=True)
        self.caption = label("", "caption")
        self.caption.setParent(host)
        self._since: float | None = None  # when the wait under way began
        for w in (self, self.caption):
            w.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            w.hide()
        line.installEventFilter(self)
        self._poll = QTimer(self)  # runs while the line is on screen
        self._poll.setInterval(self.POLL_MS)
        self._poll.timeout.connect(self.check)

    def check(self) -> None:
        what = self._waiting()
        if what is None:
            self._since = None
            self.hide()
            self.caption.hide()
            return
        now = time.monotonic()
        if self._since is None:
            self._since = now
        if now - self._since < self.DELAY:
            return
        self.caption.setText(what)
        appearing = self.isHidden()
        self._place()
        if appearing:
            self.show()
            motion.fade_in(self)
        self.raise_()  # over a crossfade's picture of the page, too
        if self.caption.isVisible():
            self.caption.raise_()

    def eventFilter(self, obj, event) -> bool:
        kind = event.type()
        if kind in (QEvent.Type.Move, QEvent.Type.Resize) and self.isVisible():
            self._place()
        elif kind == QEvent.Type.Show:
            self._poll.start()
        elif kind == QEvent.Type.Hide:
            self._poll.stop()
            self._since = None
            self.hide()
            self.caption.hide()
        return False

    def _place(self) -> None:
        g = self.line.geometry()
        self.setGeometry(g.x() + self.inset, g.center().y() - 1, max(g.width() - 2 * self.inset, 0), 3)
        size = self.caption.sizeHint()
        x = g.right() - self.inset - size.width()
        if self.beside is not None:  # in its row, past its text
            row = self.beside.geometry()
            y = row.center().y() - size.height() // 2
            room = x - row.x() - self.beside.fontMetrics().horizontalAdvance(self.beside.full_text()) >= 24
        else:  # just above the line
            y, room = g.y() - size.height() - 2, True
        self.caption.setGeometry(x, y, size.width(), size.height())
        fits = bool(self.caption.text()) and room and x > g.x()
        if fits and self.caption.isHidden():
            self.caption.show()
            motion.fade_in(self.caption)
        elif not fits:
            self.caption.hide()


class CountingLabel(QLabel):
    """A number that counts up or down to each new value instead of jumping. text() is the new value at
    once; only what's drawn counts."""

    def __init__(self, text: str, role: str):
        super().__init__(text)
        self.setProperty("role", role)
        self._final = text
        self._fmt: Callable[[float], str] = str
        self._count = motion.Tween(self._draw, self)

    def setText(self, text: str) -> None:
        """Show text as it is (a dash, a word); the next number starts from there without counting."""
        self._count.stop()
        self._count.value = None
        self._final = text
        super().setText(text)

    def set_number(self, value: float, fmt: Callable[[float], str]) -> None:
        self._fmt = fmt
        self._final = fmt(value)
        if self._count.value is None:  # was showing text, not a number: no number to count from
            super().setText(self._final)
            self._count.value = value
            self._count.target = value
            return
        self._count.set(value)

    def text(self) -> str:
        return self._final

    def _draw(self, value: float) -> None:
        super().setText(self._final if value == self._count.target else self._fmt(value))


class StatTile(QFrame):
    def __init__(self, caption: str, meter: str | None = None):
        super().__init__()
        mark(self, "glass", "tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        self.value = CountingLabel("–", "value")
        lay.addWidget(label(caption, "caption"))
        lay.addWidget(self.value)
        # Tiles without a meter keep an empty slot its size, so every caption and value lines up.
        self.meter = ShareBar(meter) if meter else None
        lay.addSpacing(4)
        if self.meter:
            lay.addWidget(self.meter)
        else:
            slot = QWidget()
            slot.setFixedHeight(6)
            lay.addWidget(slot)

    def set(self, text: str, share: float | None = None) -> None:
        self.value.setText(text)
        if self.meter is not None:
            self.meter.set(share or 0.0, animate=True)

    def set_number(self, value: float, fmt: Callable[[float], str], share: float | None = None) -> None:
        """Show a number; from a number already shown it counts to the new one and the meter eases along."""
        self.value.set_number(value, fmt)
        if self.meter is not None:
            self.meter.set(share or 0.0, animate=True)



class Calendar(QCalendarWidget):
    """A date field's popup calendar, drawn like the header's day picker: a rounded card with a soft shadow,
    weekday initials, rounded day cells with the chosen day filled, today in the accent, and days of other
    months or out of range faint."""

    SHADOW = 12  # px around the card for its shadow, as the day picker
    CELL = QSize(44, 40)  # a 40x36 day with a 4 px gap
    HEAD = 22  # weekday initials

    def __init__(self):
        super().__init__()
        self.setGridVisible(False)
        self.setVerticalHeaderFormat(QCalendarWidget.VerticalHeaderFormat.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.HorizontalHeaderFormat.NoHorizontalHeader)
        self._hover = QPoint(-1, -1)
        self.view = self.findChild(QTableView, "qt_calendar_calendarview")
        self.view.setFixedSize(7 * self.CELL.width(), 6 * self.CELL.height())
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().setAutoFillBackground(False)
        self.setAutoFillBackground(False)  # paintEvent draws the card
        self.findChild(QWidget, "qt_calendar_navigationbar").setAutoFillBackground(False)
        self.view.viewport().installEventFilter(self)
        for name in ("qt_calendar_prevmonth", "qt_calendar_nextmonth"):
            button = self.findChild(QToolButton, name)
            button.setFixedSize(30, 30)
            button.setIconSize(QSize(12, 12))
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = self.layout()
        s = self.SHADOW
        lay.setContentsMargins(s + 10, s + 10, s + 10, s + 10)
        lay.setSpacing(6)
        lay.insertSpacing(1, self.HEAD)  # paintEvent writes the weekday initials here
        self.setFixedSize(lay.sizeHint())

    def sizeHint(self) -> QSize:
        return self.size()

    def minimumSizeHint(self) -> QSize:
        return self.size()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.MouseMove:
            self._hover = event.position().toPoint()
            self.view.viewport().update()
        elif event.type() == QEvent.Type.Leave:
            self._hover = QPoint(-1, -1)
            self.view.viewport().update()
        return False

    def showEvent(self, event) -> None:
        # The date field places its popup's corner under its own; line the card up with the field instead.
        super().showEvent(event)
        popup, edit = self.window(), self.window().parentWidget()
        if popup is not self and edit is not None:
            above = popup.y() < edit.mapToGlobal(QPoint(0, 0)).y()
            s = self.SHADOW
            popup.move(popup.x() - s, popup.y() + (s - 4 if above else 4 - s))

    def paintEvent(self, event) -> None:
        theme.paint_popup(self, self.SHADOW)
        p = QPainter(self)
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        small.setWeight(QFont.Weight.DemiBold)
        p.setFont(small)
        p.setPen(QColor(theme.tokens()["muted"]))
        start = self.firstDayOfWeek().value - 1
        top = self.view.y() - self.HEAD - self.layout().spacing()
        for col in range(7):
            r = QRect(self.view.x() + col * self.CELL.width(), top, self.CELL.width(), self.HEAD)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, WEEKDAYS[(start + col) % 7])

    def paintCell(self, p: QPainter, rect: QRect, day: QDate) -> None:
        t = theme.tokens()
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(rect).adjusted(2.5, 2.5, -2.5, -2.5)
        path = QPainterPath()
        path.addRoundedRect(r, 9, 9)
        allowed = self.minimumDate() <= day <= self.maximumDate()
        chosen = day == self.selectedDate()
        hover = allowed and rect.contains(self._hover)
        if chosen:
            p.fillPath(path, QColor(t["button"]))
            if hover:
                p.fillPath(path, QColor(255, 255, 255, 30))
        elif hover:
            p.fillPath(path, QColor(t["soft"]))
        this_month = (day.year(), day.month()) == (self.yearShown(), self.monthShown())
        if chosen:
            color = t["on_accent"]
        elif not (allowed and this_month):
            color = t["faint"]
        else:
            color = t["accent"] if day == QDate.currentDate() else t["text"]
        number = QFont(self.font())
        number.setWeight(QFont.Weight.DemiBold)
        p.setFont(number)
        p.setPen(QColor(color))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, str(day.day()))
        p.restore()


class DateEdit(QDateEdit):
    """A date field whose calendar pops up as a Calendar card."""

    def __init__(self):
        super().__init__()
        self.setCalendarPopup(True)
        self.setCalendarWidget(Calendar())
        popup = self.calendarWidget().window()  # the popup window exists now; it is first shown later
        popup.setWindowFlag(Qt.WindowType.NoDropShadowWindowHint)
        popup.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
