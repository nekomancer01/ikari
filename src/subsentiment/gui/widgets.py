"""Reusable widgets and small layout helpers shared by the main window and the Download coverage window.

Nothing here knows about ikari's data; the windows put these together.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractScrollArea, QComboBox, QFrame, QLabel, QScrollArea, QVBoxLayout, QWidget,
)

from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
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
    bar is green."""

    def __init__(self, color_key: str | None = None):
        super().__init__()
        self.color_key = color_key
        self.share: float | None = None
        self.complete = False
        self._drawn = motion.Tween(lambda _: self.update(), self)  # the share on screen, easing to self.share
        self.setFixedHeight(6)

    def set(self, share: float | None, complete: bool = False, animate: bool = False) -> None:
        """With animate, the bar grows or shrinks to its new length instead of jumping there."""
        self.share = None if share is None else max(0.0, min(1.0, share))
        self.complete = complete
        self._drawn.set(self.share or 0.0, animate)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        t = theme.tokens()
        p.setBrush(QColor(t["soft"]))
        p.drawRoundedRect(r, 3, 3)
        share = self._drawn.value
        if not share:
            return
        fill = QRectF(r.x(), r.y(), max(r.height(), r.width() * share), r.height())
        if self.complete or self.color_key:
            p.setBrush(QColor(palette()["positive" if self.complete else self.color_key]))
        else:
            p.setBrush(QColor(t["accent"]))
        p.drawRoundedRect(fill, 3, 3)


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
