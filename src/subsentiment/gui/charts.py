"""The overview tab's charts, both painted here: the label split and the daily timeline.

Colors follow the polarity job: blue = positive, orange = negative, neutral gray midpoint, with
separate steps for light and dark surfaces. Blue and orange stay apart for color-blind viewers, and
every bar is also labelled by category on the axis and by value, never by color alone.
"""
from __future__ import annotations

from datetime import datetime, timezone

import bisect
import math
from functools import partial

from PySide6.QtCore import QEvent, QLineF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QToolTip, QWidget

from subsentiment.gui import motion, theme
from subsentiment.gui.days import DAY, flat_button, short_date

LIGHT = {"positive": "#0163ff", "neutral": "#b4b4b8", "negative": "#f28513", "line": "#0163ff"}
DARK = {"positive": "#3d86ff", "neutral": "#6e6e74", "negative": "#f59a3c", "line": "#3d86ff"}


def palette() -> dict:
    """Series colors for the current theme, with text, grid and card surface from the theme."""
    t = theme.tokens()
    grid = QColor(*t["edge"][:3])
    grid.setAlpha(t["edge"][3])
    return dict(DARK if theme.mode() == "dark" else LIGHT, surface=t["panel"], text=t["text"], muted=t["muted"],
                grid=grid.name(QColor.NameFormat.HexArgb))


def _ui_font(point_size: float) -> QFont:
    """The app's UI font (Inter, tabular figures) at a chart-label size."""
    f = QFont(QApplication.font())
    f.setPointSizeF(point_size)
    return f


class DistributionChart(QWidget):
    """How the items split across the labels: one segmented bar for the whole, and a row per label with
    its count and share. The segments and the rows' bars ease to new counts instead of jumping."""

    CATEGORIES = ("negative", "neutral", "positive")
    BAR, GAP, RADIUS = 40, 4, 6  # the segmented bar's height, the gap between segments, their corners
    ROW_BAR = 6  # each row's own share bar

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self._counts: dict[str, int] = {c: 0 for c in self.CATEGORIES}
        # Counts ease from those on screen (_from) to the new ones (_to) as this runs from 0 to 1.
        self._shown = [0.0] * len(self.CATEGORIES)
        self._from: list[float] = list(self._shown)
        self._to: list[float] = list(self._shown)
        self._grow = motion.Tween(self._grow_step, self)

    def set_counts(self, counts: dict[str, int]) -> None:
        """Show new counts: the text changes at once, the bars ease there."""
        self._counts = {c: int(counts.get(c, 0)) for c in self.CATEGORIES}
        self._from = list(self._shown)
        self._to = [float(self._counts[c]) for c in self.CATEGORIES]
        if self._from == self._to:
            self.update()
            return
        self._grow.value = 0.0
        self._grow.set(1.0)

    def _grow_step(self, t: float) -> None:
        self._shown = [a + (b - a) * t for a, b in zip(self._from, self._to)]
        self.update()

    def shown_heights(self) -> list[float]:
        """The counts the bars are drawn at right now (for tests)."""
        return list(self._shown)

    def _layout(self) -> tuple[list[QRectF], list[QRectF]]:
        """The bar's segments and the rows below it, in widget coordinates."""
        r = QRectF(self.rect()).adjusted(0, 6, 0, 0)
        total = sum(self._shown)
        room = r.width() - self.GAP * (len(self.CATEGORIES) - 1)
        segments, x = [], r.left()
        for v in self._shown:
            w = room * v / total if total else 0.0
            segments.append(QRectF(x, r.top(), w, self.BAR))
            x += w + (self.GAP if w > 0.5 else 0)
        top = r.top() + self.BAR + 18
        step = (r.bottom() - top) / len(self.CATEGORIES)
        rows = [QRectF(r.left(), top + i * step, r.width(), step) for i in range(len(self.CATEGORIES))]
        return segments, rows

    def paintEvent(self, event) -> None:
        colors = palette()
        t = theme.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        segments, rows = self._layout()
        total = sum(self._shown)
        p.setPen(Qt.PenStyle.NoPen)
        if not total:  # nothing yet: the empty track, so the card isn't blank
            p.setBrush(QColor(t["soft"]))
            p.drawRoundedRect(QRectF(0, segments[0].top(), self.width(), self.BAR), self.RADIUS, self.RADIUS)
        for cat, seg in zip(self.CATEGORIES, segments):
            if seg.width() > 0.5:
                rad = min(self.RADIUS, seg.width() / 2)
                p.setBrush(QColor(colors[cat]))
                p.drawRoundedRect(seg, rad, rad)

        name_font = QFont(self.font())
        count_font = theme.display_font(self.font().pointSizeF() + 0.5, 600)
        share_font = QFont(self.font())
        whole = sum(self._counts.values())
        share_w = QFontMetricsF(share_font).horizontalAdvance("100%")
        count_w = QFontMetricsF(count_font).horizontalAdvance(f"{whole:,}")  # no count is longer than the whole
        for i, (cat, row) in enumerate(zip(self.CATEGORIES, rows)):
            mid = row.center().y()
            if i:  # a hairline between rows, as in a ruled list
                p.setPen(QPen(QColor(*t["edge"]), 1))
                p.drawLine(QPointF(row.left(), row.top() + 0.5), QPointF(row.right(), row.top() + 0.5))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(colors[cat]))
            p.drawRoundedRect(QRectF(row.left(), mid - 4, 8, 8), 2.5, 2.5)
            p.setPen(QColor(t["text"]))
            p.setFont(name_font)
            name = QRectF(row.left() + 18, row.top(), 90, row.height())
            p.drawText(name, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), cat.title())
            # The share, then the count, at the right; a thin bar of the share between them and the name.
            right = QRectF(row.right() - share_w, row.top(), share_w, row.height())
            share = 100 * self._counts[cat] / whole if whole else 0
            p.setPen(QColor(t["muted"]))
            p.drawText(right, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       f"{share:.0f}%" if whole else "–")
            count = QRectF(right.left() - 12 - count_w, row.top(), count_w, row.height())
            p.setPen(QColor(t["text"]))
            p.setFont(count_font)
            p.drawText(count, int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       f"{self._counts[cat]:,}")
            track = QRectF(name.right() + 8, mid - self.ROW_BAR / 2,
                           max(0.0, count.left() - 16 - name.right() - 8), self.ROW_BAR)
            if track.width() > 24:
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(t["soft"]))
                p.drawRoundedRect(track, 3, 3)
                drawn = self._shown[i] / total if total else 0.0
                if drawn > 0:
                    fill = QColor(colors[cat])
                    p.setBrush(fill)
                    p.drawRoundedRect(QRectF(track.left(), track.top(), max(track.height(), track.width() * drawn),
                                             track.height()), 3, 3)

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            segments, rows = self._layout()
            pos = QPointF(e.pos())
            hit = next((i for i, r in enumerate(segments) if r.contains(pos)), None)
            if hit is None:
                hit = next((i for i, r in enumerate(rows) if r.contains(pos)), None)
            if hit is None:
                QToolTip.hideText()
            else:
                cat = self.CATEGORIES[hit]
                n, whole = self._counts[cat], sum(self._counts.values())
                QToolTip.showText(e.globalPos(), f"{cat.title()}: {n:,} ({100 * n / (whole or 1):.0f}%)", self)
            return True
        return super().event(e)


# Symmetric y ranges for the timeline: the smallest that fits the days in view, so day-to-day
# movement is visible, with zero always in the middle so the sign reads at a glance.
TIMELINE_BOUNDS = (0.2, 0.4, 0.6, 1.0)
NEUTRAL_BAND = 0.05  # matches the label thresholds in sentiment.py
# The range buttons, as on SteamDB: the latest week, month, ... (in days), or every day.
PRESETS = (("1w", 7), ("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365), ("All", None))
PRESET_TIPS = {"1w": "The latest week", "1m": "The latest month", "3m": "The latest three months",
               "6m": "The latest six months", "1y": "The latest year", "All": "Every day (or double-click the chart)"}
MIN_SPAN = 3 * DAY  # the closest zoom: three days across the plot
DAY_STEPS = (1, 2, 3, 7, 14)  # days between date labels, smallest first; past these, months
MONTH_STEPS = (1, 2, 3, 6, 12, 24, 60)
MONDAY = 4  # weekly labels fall on Mondays: day 0 of the epoch, 1 Jan 1970, was a Thursday
NAV_H, NAV_GAP = 28, 12  # the navigator strip under the plot, and the space above it
VOLUME = 0.2  # how much of the plot's height the items-per-day bars may take
DOTS = 45  # with at most this many points in view, each gets a dot
GROUP_PX = 4  # days closer together than this on screen are shown a week, or a month, per point


def timeline_bound(points: list[tuple[int, float, int]]) -> float:
    peak = max((abs(mean) for _, mean, _ in points), default=0.0)
    return next((b for b in TIMELINE_BOUNDS if peak * 1.1 <= b), TIMELINE_BOUNDS[-1])


def _signed(v: float) -> str:
    return "0" if abs(v) < 1e-9 else f"{v:+.1f}".replace("-", "−")


class TimelineChart(QWidget):
    """Mean compound sentiment per day (UTC) on a dated axis, laid out like SteamDB's charts: a plot with a
    crosshair and a hover box for the day under the pointer, faint bars for how many items each day has, range
    buttons (`toolbar`, which the overview puts beside the card's caption) and a navigator strip underneath
    that always shows every day, with a window over the part in view.

    Zoom by dragging across the plot, with the mouse wheel (Shift+wheel moves sideways), with the range buttons
    or by dragging the navigator's window or its edges; double-click the plot for every day again. Zooming
    only changes what the chart shows: the header's Days picker still decides which days are counted, and the
    window calls `show_every_day` when other days are chosen there or another subreddit is shown.

    Self-contained: the overview card holds it today, and it fills a page of any width just as well.

    `view` is the span in view, [lo, hi) in epoch seconds, or None for every day, which widens as a running
    fetch adds days; a view that ends at the latest day keeps ending there as days are added."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.points: list[tuple[int, float, int]] = []
        self._days: list[int] = []  # the points' days, for finding the ones in view
        self._grouped: dict[tuple, tuple] = {}  # groups() by (lo, hi, width), for the points as they are now
        self.view: tuple[float, float] | None = None
        self.preset: str | None = "All"
        a, b = self.domain()
        self._screen = (a, b, TIMELINE_BOUNDS[0])  # the (lo, hi, y bound) drawn now, eased towards _to
        self._from = self._to = self._screen
        self._zoom = motion.Tween(self._zoom_step, self)
        self._hover: tuple | None = None  # the (unit, start, end, mean, items) under the pointer
        self._pointer: QPointF | None = None  # where the pointer is over the chart
        self._drag: tuple | None = None  # (what, x where it started, view then)
        self._select: tuple[float, float] | None = None  # the band dragged across the plot, in x pixels

        self.toolbar = QWidget()
        bar = QHBoxLayout(self.toolbar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(2)
        self.presets = {}
        for key, _ in PRESETS:
            b = flat_button(key)
            b.setCheckable(True)
            b.setToolTip(PRESET_TIPS[key])
            b.ensurePolished()  # the style's 9 pt font and padding, not a push button's usual minimum width
            b.setFixedWidth(b.fontMetrics().horizontalAdvance(key) + 20)
            b.clicked.connect(partial(self.zoom_preset, key))
            bar.addWidget(b)
            self.presets[key] = b
        self._sync_buttons()

    # -- data and view --------------------------------------------------------------------------------

    def set_points(self, points: list[tuple[int, float, int]]) -> None:
        if points == self.points:
            return  # nothing new (a running fetch asks four times a second)
        old_end = self.domain()[1]
        if self.points and self._drag is None:
            motion.crossfade(self)  # new days or new means: the old line fades into the new one
        self.points = list(points)
        self._days = [d for d, _, _ in self.points]
        self._grouped.clear()
        if self.view is not None and self.view[1] >= old_end:
            grow = self.domain()[1] - old_end  # a fetch added later days: keep showing the latest ones
            self.view = (self.view[0] + grow, self.view[1] + grow)
        self._apply(False, follow=True)

    def show_every_day(self) -> None:
        """Forget the zoom, for other data: another subreddit, or other days in the Days picker."""
        self.view, self.preset = None, "All"
        self._apply(False)

    def domain(self) -> tuple[float, float]:
        """Every day's span, from the first one's start to the end of the last one."""
        if not self._days:
            today = int(datetime.now(timezone.utc).timestamp()) // DAY * DAY
            return today, today + DAY
        return self._days[0], self._days[-1] + DAY

    def set_view(self, lo: float, hi: float, animate: bool = False, preset: str | None = None) -> None:
        """Show [lo, hi), kept inside the days there are and no narrower than MIN_SPAN."""
        a, b = self.domain()
        span = min(max(hi - lo, MIN_SPAN), b - a)
        lo = min(max(lo, a), b - span)
        self.view = None if span >= b - a else (lo, lo + span)
        self.preset = preset or ("All" if self.view is None else None)
        self._apply(animate)

    def zoom_preset(self, key: str) -> None:
        a, b = self.domain()
        days = dict(PRESETS).get(key)
        self.set_view(a if days is None else b - days * DAY, b, animate=True, preset=key)

    def visible(self, lo: float | None = None, hi: float | None = None) -> list[tuple[int, float, int]]:
        """The points with any of their day inside [lo, hi), by default the span in view."""
        if lo is None:
            lo, hi = self.view or self.domain()
        return self.points[bisect.bisect_right(self._days, lo - DAY):bisect.bisect_left(self._days, hi)]

    def groups(self, lo: float, hi: float, width: float) -> tuple[str, list[tuple[float, float, float, int]]]:
        """The points in [lo, hi) as (start, end, mean, items): a point a day, or, where days would be closer
        than GROUP_PX apart across ``width``, a week or a month a point, as SteamDB's charts group theirs. The
        unit is "day", "week" or "month"; a week or month's mean counts every item in it alike."""
        span = max(hi - lo, DAY)
        unit = next((u for u, size in (("day", DAY), ("week", 7 * DAY)) if width * size / span >= GROUP_PX), "month")
        # Keyed on whole days, which pick the same points, so painting, hovering and each frame of a zoom
        # don't group them again.
        key = (unit, math.floor(lo / DAY), math.ceil(hi / DAY))
        if key not in self._grouped:
            if len(self._grouped) > 16:
                self._grouped.clear()
            self._grouped[key] = unit, self._group(unit, lo, hi)
        return self._grouped[key]

    def _group(self, unit: str, lo: float, hi: float) -> list[tuple[float, float, float, int]]:
        if unit == "day":
            return [(d, d + DAY, mean, n) for d, mean, n in self.visible(lo, hi)]
        out: dict[float, list] = {}
        for d, mean, n in self.visible(lo - 31 * DAY, hi + 31 * DAY):  # whole weeks and months at the ends
            if unit == "week":
                start = d - (d // DAY - MONDAY) % 7 * DAY
                end = start + 7 * DAY
            else:
                when = datetime.fromtimestamp(d, timezone.utc)
                start = datetime(when.year, when.month, 1, tzinfo=timezone.utc).timestamp()
                end = datetime(when.year + when.month // 12, when.month % 12 + 1, 1, tzinfo=timezone.utc).timestamp()
            if end > lo and start < hi:
                g = out.setdefault(start, [end, 0.0, 0])
                g[1] += mean * n
                g[2] += n
        return [(start, end, total / n, n) for start, (end, total, n) in out.items() if n]

    def _draw_line(self, p: QPainter, shown: list, x_of, y_of, pen: QPen) -> None:
        """The points joined, with a gap where days are missing. Drawn as separate segments: Qt strokes one
        long zigzag path very slowly (most of a second for ten years of days)."""
        segments, alone, prev = [], [], None
        for i, (start, end, mean, _) in enumerate(shown):
            pt = QPointF(x_of((start + end) / 2), y_of(mean))
            if prev is not None and shown[i - 1][1] >= start:
                segments.append(QLineF(prev, pt))
            elif i + 1 == len(shown) or shown[i + 1][0] > end:
                alone.append(pt)  # no neighbour on either side: a dot, or it wouldn't show at all
            prev = pt
        p.setPen(pen)
        p.drawLines(segments)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(pen.color())
        for pt in alone:
            p.drawEllipse(pt, pen.widthF(), pen.widthF())

    def _apply(self, animate: bool, follow: bool = False) -> None:
        """Ease (or jump) to the view. With follow (new data), a zoom that is easing carries on, its start
        moved along with its target, so it neither restarts nor jumps."""
        if self.view is not None:  # the days may have changed under it
            a, b = self.domain()
            span = min(self.view[1] - self.view[0], b - a)
            lo = min(max(self.view[0], a), b - span)
            self.view = None if span >= b - a else (lo, lo + span)
        lo, hi = self.view or self.domain()
        _, shown = self.groups(lo, hi, self._areas()[0].width())
        old, self._to = self._to, (lo, hi, timeline_bound([(g[0], g[2], g[3]) for g in shown]))
        self._sync_buttons()
        if follow and self._zoom.running():
            self._from = tuple(f + (b - a) for f, a, b in zip(self._from, old, self._to))
        elif animate and motion.enabled():
            self._from = self._screen
            self._zoom.value = 0.0
            self._zoom.set(1.0)
        else:
            self._zoom.stop()
            self._screen = self._to
            self.update()
        if self._pointer is not None:  # the box follows new means and a new grouping under a still pointer
            self._hover_at(self._pointer, self._areas()[0])

    def _zoom_step(self, f: float) -> None:
        self._screen = tuple(a + (b - a) * f for a, b in zip(self._from, self._to))
        self.update()

    def _sync_buttons(self) -> None:
        a, b = self.domain()
        for key, days in PRESETS:
            button = self.presets[key]
            button.setEnabled(days is None or days * DAY < b - a)  # a span longer than the days there are is All
            button.setChecked(key == self.preset)

    # -- geometry ---------------------------------------------------------------------------------------

    def _areas(self) -> tuple[QRectF, QRectF]:
        """The plot and the navigator under it, in widget coordinates."""
        fm = QFontMetricsF(_ui_font(9))
        left = fm.horizontalAdvance("−0.6") + 10
        bottom = self.height() - NAV_H - 1
        nav = QRectF(left, bottom, max(1.0, self.width() - left - 2), NAV_H)
        plot = QRectF(left, 8, nav.width(), max(1.0, bottom - NAV_GAP - fm.height() - 8 - 8))
        return plot, nav

    @staticmethod
    def _x(t: float, lo: float, hi: float, r: QRectF) -> float:
        return r.left() + (t - lo) / (hi - lo) * r.width()

    @staticmethod
    def _t(x: float, lo: float, hi: float, r: QRectF) -> float:
        return lo + (x - r.left()) / r.width() * (hi - lo)

    def _ticks(self, lo: float, hi: float, width: float) -> list[tuple[float, str]]:
        """Date labels for [lo, hi): (the labelled day's start, label), stepped so they never crowd."""
        fm = QFontMetricsF(_ui_font(9))
        span = (hi - lo) / DAY
        room = width / (fm.horizontalAdvance("30 Sep") + 20)
        first, last = math.floor(lo / DAY), math.ceil(hi / DAY)
        for step in DAY_STEPS:
            if span / step <= room:
                offset = MONDAY if step >= 7 else 0
                return [(d * DAY, short_date(d * DAY)) for d in range(first, last) if (d - offset) % step == 0]
        room = width / (fm.horizontalAdvance("2026") + 24)
        step = next((s for s in MONTH_STEPS if span / 30.44 / s <= room), MONTH_STEPS[-1])
        start = datetime.fromtimestamp(lo, timezone.utc)
        y, m, out = start.year, start.month, []
        while True:
            t = datetime(y, m, 1, tzinfo=timezone.utc).timestamp()
            if t >= hi:
                return out
            if t >= lo and ((y * 12 + m - 1) % step == 0):
                out.append((t, str(y) if m == 1 or step >= 12 else f"{datetime(y, m, 1):%b}"))
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)

    # -- painting ---------------------------------------------------------------------------------------

    def paintEvent(self, event) -> None:
        colors, t = palette(), theme.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = _ui_font(9)
        p.setFont(font)
        fm = QFontMetricsF(font)
        plot, nav = self._areas()
        lo, hi, bound = self._screen

        def y_of(v: float) -> float:
            return plot.center().y() - v / bound * plot.height() / 2

        # The scale: gridlines and signed labels, a faint band for the neutral zone, a dashed zero.
        muted, grid = QColor(colors["muted"]), QColor(colors["grid"])
        for v in (-bound, -bound / 2, 0.0, bound / 2, bound):
            y = y_of(v)
            if v:
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            p.setPen(muted)
            p.drawText(QRectF(0, y - fm.height() / 2, plot.left() - 8, fm.height()),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter), _signed(v))
        band = QColor(colors["neutral"])
        band.setAlpha(30)
        p.fillRect(QRectF(plot.left(), y_of(NEUTRAL_BAND), plot.width(), y_of(-NEUTRAL_BAND) - y_of(NEUTRAL_BAND)),
                   band)
        zero = QPen(muted, 1, Qt.PenStyle.DashLine)
        p.setPen(zero)
        p.drawLine(QPointF(plot.left(), y_of(0)), QPointF(plot.right(), y_of(0)))

        # Dates under the plot, each under its day's point.
        p.setPen(muted)
        for start, text in self._ticks(lo, hi, plot.width()):
            x = self._x(start + DAY / 2, lo, hi, plot)
            w = fm.horizontalAdvance(text)
            if plot.left() - 4 <= x <= plot.right() + 4 and x - w / 2 >= 0 and x + w / 2 <= self.width():
                p.setPen(QPen(grid, 1))
                p.drawLine(QPointF(x, plot.bottom()), QPointF(x, plot.bottom() + 4))
                p.setPen(muted)
                p.drawText(QRectF(x - w / 2 - 2, plot.bottom() + 6, w + 4, fm.height()),
                           int(Qt.AlignmentFlag.AlignCenter), text)

        p.save()
        p.setClipRect(plot.adjusted(-6, -6, 6, 6))
        _, shown = self.groups(lo, hi, plot.width())
        line = QColor(colors["line"])

        def x_of(at: float) -> float:
            return self._x(at, lo, hi, plot)
        # Items per point as faint bars along the bottom, so a mean from a handful of items reads as such.
        most = max((n for *_, n in shown), default=0)
        if most:
            bar = QColor(colors["neutral"])
            bar.setAlpha(55 if theme.mode() == "dark" else 70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(bar)
            for start, end, _, n in shown:
                h = max(1.0, n / most * plot.height() * VOLUME)
                x0, x1 = x_of(start), x_of(end)
                bw = max(1.0, min((x1 - x0) * 0.6, 14))
                p.drawRect(QRectF((x0 + x1 - bw) / 2, plot.bottom() - h, bw, h))
        self._draw_line(p, shown, x_of, y_of, QPen(line, 2.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        ring = QPen(QColor(colors["surface"]), 1.5)
        if len(shown) <= DOTS:
            p.setPen(ring)
            p.setBrush(line)
            for start, end, mean, _ in shown:
                p.drawEllipse(QPointF(x_of((start + end) / 2), y_of(mean)), 4, 4)
        if self._select:
            a, b = sorted(self._select)
            sel = QColor(t["accent"])
            sel.setAlpha(36)
            p.fillRect(QRectF(a, plot.top(), b - a, plot.height()), sel)
        p.restore()

        if self._hover and not self._select:
            unit, start, end, mean, n = self._hover
            x, y = x_of((start + end) / 2), y_of(mean)
            if plot.left() - 1 <= x <= plot.right() + 1:
                p.setPen(QPen(muted, 1))
                p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                ring.setWidthF(2)
                p.setPen(ring)
                p.setBrush(line)
                p.drawEllipse(QPointF(x, y), 5.5, 5.5)
                self._paint_box(p, plot, x, self._hover)
        self._paint_navigator(p, nav, colors, t)

    def _paint_box(self, p: QPainter, plot: QRectF, x: float, hover: tuple) -> None:
        """The hover box: the day (or week, or month), its mean and how many items, beside the crosshair."""
        unit, start, _, mean, n = hover
        t = theme.tokens()
        head = QFont(self.font())
        head.setWeight(QFont.Weight.DemiBold)
        body = QFont(self.font())
        when = datetime.fromtimestamp(start, timezone.utc)
        title = {"day": f"{when:%a} {when.day} {when:%b %Y}", "week": f"Week of {when.day} {when:%b %Y}",
                 "month": f"{when:%B %Y}"}[unit]
        lines = [(head, title),
                 (body, f"Mean  {mean:+.3f}".replace("-", "−")),
                 (body, f"Items  {n:,}")]
        fms = [QFontMetricsF(f) for f, _ in lines]
        w = max(fm.horizontalAdvance(text) for fm, (_, text) in zip(fms, lines)) + 20
        h = sum(fm.height() for fm in fms) + 4 * (len(lines) - 1) + 16
        left = x + 12 if x + 12 + w <= plot.right() else x - 12 - w
        box = QRectF(max(0.0, left), plot.top() + 4, w, h)
        p.setPen(QPen(QColor(*t["edge"]), 1))
        p.setBrush(QColor(t["menu"]))
        p.drawRoundedRect(box, 6, 6)
        top = box.top() + 8
        for fm, (f, text) in zip(fms, lines):
            p.setFont(f)
            p.setPen(QColor(t["text"] if f is head else t["muted"]))
            p.drawText(QRectF(box.left() + 10, top, w - 20, fm.height()),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)
            top += fm.height() + 4

    def _paint_navigator(self, p: QPainter, nav: QRectF, colors: dict, t: dict) -> None:
        """Every day in small, with the span in view as a window that can be dragged or resized."""
        a, b = self.domain()
        lo, hi, _ = self._screen
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(t["soft"]))
        p.drawRoundedRect(nav, 6, 6)
        _, every = self.groups(a, b, nav.width() * 2)  # a thin line: days can sit twice as close
        bound = timeline_bound([(g[0], g[2], g[3]) for g in every])
        mid = nav.center().y()
        line = QColor(colors["line"])
        line.setAlpha(150)
        self._draw_line(p, every, lambda at: self._x(at, a, b, nav),
                        lambda v: mid - v / bound * (nav.height() / 2 - 3), QPen(line, 1.2))
        x0, x1 = self._x(lo, a, b, nav), self._x(hi, a, b, nav)
        window = QRectF(x0, nav.top(), max(2.0, x1 - x0), nav.height())
        fill = QColor(t["accent"])
        fill.setAlpha(30)
        edge = QColor(t["accent"])
        edge.setAlpha(150)
        p.setPen(QPen(edge, 1))
        p.setBrush(fill)
        p.drawRoundedRect(window.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)
        for x in (window.left(), window.right()):  # the grips on each edge
            grip = QRectF(x - 4, mid - 8, 8, 16)
            p.setPen(QPen(QColor(*t["edge"]), 1))
            p.setBrush(QColor(t["panel"]))
            p.drawRoundedRect(grip, 3, 3)
            p.setPen(QPen(QColor(colors["muted"]), 1))
            for dx in (-1.5, 1.5):
                p.drawLine(QPointF(x + dx, mid - 3.5), QPointF(x + dx, mid + 3.5))

    # -- pointer --------------------------------------------------------------------------------------

    def _nav_part(self, x: float) -> str:
        """Which part of the navigator x is over: the window's left or right edge, the window, or outside."""
        nav = self._areas()[1]
        a, b = self.domain()
        lo, hi, _ = self._screen
        x0, x1 = self._x(lo, a, b, nav), self._x(hi, a, b, nav)
        if abs(x - x0) <= 6 and x < x1 - 2:
            return "left"
        if abs(x - x1) <= 6:
            return "right"
        return "move" if x0 < x < x1 else "outside"

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        plot, nav = self._areas()
        pos = event.position()
        if nav.adjusted(0, -4, 0, 4).contains(pos):
            part = self._nav_part(pos.x())
            if part == "outside":  # jump there: the window centres on the click, then follows the drag
                a, b = self.domain()
                lo, hi = self.view or (a, b)
                mid = self._t(pos.x(), a, b, nav)
                self.set_view(mid - (hi - lo) / 2, mid + (hi - lo) / 2)
                part = "move"
            self._drag = (part, pos.x(), self.view or self.domain())
            self.setCursor(Qt.CursorShape.ClosedHandCursor if part == "move" else Qt.CursorShape.SizeHorCursor)
        elif plot.contains(pos):
            self._drag = ("select", pos.x(), None)
            self._select = (pos.x(), pos.x())
            self.update()

    def mouseMoveEvent(self, event) -> None:
        plot, nav = self._areas()
        pos = event.position()
        if self._drag is None:
            self._hover_at(pos, plot)
            part = self._nav_part(pos.x()) if nav.adjusted(0, -4, 0, 4).contains(pos) else None
            self.setCursor({"left": Qt.CursorShape.SizeHorCursor, "right": Qt.CursorShape.SizeHorCursor,
                            "move": Qt.CursorShape.OpenHandCursor}.get(part, Qt.CursorShape.ArrowCursor))
            return
        what, x, then = self._drag
        if what == "select":
            self._select = (self._select[0], min(max(pos.x(), plot.left()), plot.right()))
            self.update()
            return
        lo, hi = then
        a, b = self.domain()
        dt = (pos.x() - x) / nav.width() * (b - a)
        if what == "move":
            self.set_view(lo + dt, hi + dt)
        elif what == "left":  # held inside the days there are, so the other edge stays put
            self.set_view(max(a, min(lo + dt, hi - MIN_SPAN)), hi)
        else:
            self.set_view(lo, min(b, max(hi + dt, lo + MIN_SPAN)))

    def mouseReleaseEvent(self, event) -> None:
        if self._drag and self._drag[0] == "select" and self._select:
            plot, _ = self._areas()
            x0, x1 = sorted(self._select)
            if x1 - x0 > 6:  # a drag, not a click: zoom to the days it covers
                lo, hi, _ = self._screen
                self.set_view(self._t(x0, lo, hi, plot), self._t(x1, lo, hi, plot), animate=True)
        self._drag = self._select = None
        self.unsetCursor()
        self.update()

    def mouseDoubleClickEvent(self, event) -> None:
        if self._areas()[0].contains(event.position()):
            self.zoom_preset("All")

    def wheelEvent(self, event) -> None:
        plot, nav = self._areas()
        lo, hi = self.view or self.domain()
        d = event.angleDelta()
        sideways = d.x() or (d.y() if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else 0)
        if sideways:
            shift = -sideways / 120 * (hi - lo) * 0.15
            self.set_view(lo + shift, hi + shift)
        elif d.y() < 0 and self.view is None:
            event.ignore()  # every day shown already: the page under the chart scrolls instead
            return
        elif d.y():
            on_nav = nav.contains(event.position())
            a, b = self.domain()
            at = self._t(event.position().x(), *((a, b) if on_nav else (lo, hi)), nav if on_nav else plot)
            at = min(max(at, lo), hi)
            f = 0.8 ** (d.y() / 120)  # up zooms in, round the day under the pointer
            self.set_view(at - (at - lo) * f, at + (hi - at) * f)
        self._hover_at(event.position(), plot)
        event.accept()

    def leaveEvent(self, event) -> None:
        self._hover = self._pointer = None
        self.update()

    def resizeEvent(self, event) -> None:
        self._apply(False, follow=True)  # the grouping, and so the y scale, depend on the width

    def _hover_at(self, pos: QPointF, plot: QRectF) -> None:
        """Hover the point nearest the pointer, if it's over the plot."""
        self._pointer, hover = pos, None
        if plot.adjusted(0, 0, 0, 6).contains(pos) and self.points:
            lo, hi, _ = self._screen
            at = self._t(pos.x(), lo, hi, plot)
            unit, shown = self.groups(lo, hi, plot.width())
            if shown:
                hover = (unit, *min(shown, key=lambda g: abs((g[0] + g[1]) / 2 - at)))
        if hover != self._hover:
            self._hover = hover
            self.update()

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            return True  # the hover box shows the day instead
        return super().event(e)
