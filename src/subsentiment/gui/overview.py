"""The Overview page (four stat tiles, the timeline across the full width, the distribution under it) and
the single-card pages beside it in the rail: Timeline and Words."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from subsentiment.analysis import Analysis
from subsentiment.gui.charts import DistributionChart, TimelineChart
from subsentiment.gui.widgets import ElidedLabel, StatTile, VerticalScrollArea, label, panel


def card(title: str, body: QWidget, extra: QWidget | None = None) -> QFrame:
    """A titled card on a page; ``extra`` (the timeline's range buttons) sits at the right of the title, and
    the title gives way to it on a narrow window."""
    frame, inner = panel("tile", margins=14, spacing=4)
    head = QHBoxLayout()
    caption = ElidedLabel(title, "caption") if extra is not None else label(title, "caption")
    head.addWidget(caption)
    head.addStretch(1)
    if extra is not None:
        caption.setMinimumHeight(extra.sizeHint().height())
        head.addWidget(extra)
    inner.addLayout(head)
    inner.addWidget(body, 1)
    return frame


class CardPage(QWidget):
    """A page that is one titled card filling the content area, such as Timeline and Words."""

    def __init__(self, title: str, body: QWidget, extra: QWidget | None = None):
        super().__init__()
        self.body = body
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card(title, body, extra), 1)
        self.cards = [body.parentWidget()]


def coming_soon(text: str) -> QLabel:
    note = label(text, "muted")
    note.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return note


class OverviewPage(VerticalScrollArea):
    """On a short window (1366x768 or 1280x800 at 125%) the overview scrolls rather than holding the
    window taller than the screen or squeezing the charts."""

    def __init__(self):
        super().__init__()
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.tiles = {
            "count": StatTile("Items analysed"),
            "mean": StatTile("Mean sentiment"),
            "positive": StatTile("Positive", meter="positive"),
            "negative": StatTile("Negative", meter="negative"),
        }
        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        widest = max(t.minimumSizeHint().width() for t in self.tiles.values())
        for t in self.tiles.values():
            t.setMinimumWidth(widest)  # equal widths, whatever the caption length, down to the narrowest window
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)

        self.timeline = TimelineChart()
        self.distribution = DistributionChart()
        # Shorter than on a page of their own, so the whole Overview fits a 1920x1200 150% window unscrolled.
        self.timeline.setMinimumHeight(170)
        self.distribution.setMinimumHeight(120)
        self.words = coming_soon("The word cloud goes here.")
        lay.addWidget(card("Mean sentiment per day (UTC)", self.timeline, self.timeline.toolbar), 5)
        bottom = QHBoxLayout()
        bottom.setSpacing(14)
        bottom.addWidget(card("Sentiment distribution", self.distribution), 2)
        bottom.addWidget(card("Words", self.words), 3)
        lay.addLayout(bottom, 4)

        # The page's cards.
        self.cards = [*self.tiles.values(), self.timeline.parentWidget(), self.distribution.parentWidget(),
                      self.words.parentWidget()]
        self.setWidget(page)

    def show_analysis(self, a: Analysis) -> None:
        n = a.count
        pos, neg = a.share("positive"), a.share("negative")
        # From a number already shown, each tile counts to its new one rather than jumping.
        self.tiles["count"].set_number(n, lambda v: f"{round(v):,}")
        if n:
            self.tiles["mean"].set_number(a.mean, lambda v: f"{v:+.3f}")
            self.tiles["positive"].set_number(100 * pos, lambda v: f"{v:.0f}%", pos)
            self.tiles["negative"].set_number(100 * neg, lambda v: f"{v:.0f}%", neg)
        else:
            for key in ("mean", "positive", "negative"):
                self.tiles[key].set("–")
        self.distribution.set_counts(a.counts)
        self.timeline.set_points(a.daily_means)
