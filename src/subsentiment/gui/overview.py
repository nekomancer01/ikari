"""The Overview tab: four stat tiles, and the distribution and timeline charts."""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from subsentiment.analysis import Analysis
from subsentiment.gui.charts import DistributionChart, TimelineChart
from subsentiment.gui.widgets import StatTile, VerticalScrollArea, label, panel


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

        self.distribution = DistributionChart()
        self.timeline = TimelineChart()
        charts = QHBoxLayout()
        charts.setSpacing(14)
        for chart, title, stretch in ((self.distribution, "Sentiment distribution", 2),
                                      (self.timeline, "Mean sentiment per day (UTC)", 3)):
            card, inner = panel("tile", margins=14, spacing=4)
            inner.addWidget(label(title, "caption"))
            inner.addWidget(chart, 1)
            charts.addWidget(card, stretch)
        lay.addLayout(charts, 3)

        # What fades in, one after another, when another subreddit is shown.
        self.cards = [*self.tiles.values(), self.distribution.parentWidget(), self.timeline.parentWidget()]
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
