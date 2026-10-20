"""The Overview tab: four stat tiles, the distribution and timeline charts, and the strongest items."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from subsentiment.analysis import Analysis
from subsentiment.gui import motion
from subsentiment.gui.charts import DistributionChart, TimelineChart
from subsentiment.gui.widgets import QuietScrollBars, StatTile, VerticalScrollArea, label, panel


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
        for chart, stretch in ((self.distribution, 2), (self.timeline, 3)):
            card, inner = panel("true", margins=10)
            inner.addWidget(chart)
            charts.addWidget(card, stretch)
        lay.addLayout(charts, 3)

        self.most_positive = QListWidget()
        self.most_negative = QListWidget()
        extremes = QHBoxLayout()
        extremes.setSpacing(14)
        for title, lw in (("Most positive", self.most_positive), ("Most negative", self.most_negative)):
            lw.setFrameShape(QFrame.Shape.NoFrame)
            # Long titles end in "…" (the tooltip has the rest) instead of adding a sideways scrollbar.
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            lw.setTextElideMode(Qt.TextElideMode.ElideRight)
            lw.setUniformItemSizes(True)
            QuietScrollBars(lw)
            lw.itemDoubleClicked.connect(
                lambda item: QDesktopServices.openUrl(QUrl(item.data(Qt.ItemDataRole.UserRole))))
            card, inner = panel("solid", margins=14, spacing=6)
            inner.addWidget(label(title, "muted"))
            inner.addWidget(lw)
            extremes.addWidget(card)
        lay.addLayout(extremes, 2)
        # What fades in, one after another, when another subreddit is shown.
        self.cards = [*self.tiles.values(), *(self.distribution.parentWidget(), self.timeline.parentWidget()),
                      *(lw.parentWidget() for lw in (self.most_positive, self.most_negative))]
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
        for widget, rows in ((self.most_positive, a.most_positive()), (self.most_negative, a.most_negative())):
            entries = []
            for r in rows:
                text = r["title"] if "title" in r.keys() else r["body"]
                text = " ".join((text or "").split())
                entries.append((f"{r['sentiment']:+.2f}    {text[:140]}", text[:1000], r["permalink"]))
            if [e[0] for e in entries] == [widget.item(i).text() for i in range(widget.count())]:
                continue  # the same items: nothing to redo (a running fetch asks once a second)
            motion.crossfade(widget.viewport())
            widget.clear()
            for shown, tip, link in entries:
                item = QListWidgetItem(shown)
                item.setToolTip(tip)
                item.setData(Qt.ItemDataRole.UserRole, link)
                widget.addItem(item)
