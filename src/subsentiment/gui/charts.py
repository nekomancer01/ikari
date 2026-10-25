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

from PySide6.QtCore import QEvent, QLineF, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QFontMetricsF, QPainter, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QToolTip, QVBoxLayout, QWidget,
)

from subsentiment.analysis import LABELS
from subsentiment.gui import motion, theme
from subsentiment.gui.days import DAY, flat_button, short_date

def palette() -> dict:
    """Series colors, text, grid and card surface, all from the current theme."""
    t = theme.tokens()
    grid = QColor(*t["edge"][:3])
    grid.setAlpha(t["edge"][3])
    return {"positive": t["positive"], "neutral": t["neutral"], "negative": t["negative"], "line": t["positive"],
            "surface": t["panel"], "text": t["text"], "muted": t["muted"], "grid": grid.name(QColor.NameFormat.HexArgb)}


def _ui_font(point_size: float) -> QFont:
    """The app's UI font (Inter, tabular figures) at a chart-label size."""
    f = QFont(QApplication.font())
    f.setPointSizeF(point_size)
    return f


class DistributionChart(theme.OnCard):
    """How the items split across the labels: one segmented bar for the whole, and a row per label with
    its count and share. The segments and the rows' bars ease to new counts instead of jumping."""

    CATEGORIES = LABELS
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
        p = self.painter()
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
NOTE_WRAP = 240  # the widest a note's description runs in the hover box before wrapping


def timeline_bound(points: list[tuple[int, float, int]]) -> float:
    peak = max((abs(mean) for _, mean, _ in points), default=0.0)
    return next((b for b in TIMELINE_BOUNDS if peak * 1.1 <= b), TIMELINE_BOUNDS[-1])


def _signed(v: float) -> str:
    return "0" if abs(v) < 1e-9 else f"{v:+.1f}".replace("-", "−")


class TimelineChart(theme.OnCard):
    """Mean compound sentiment per day (UTC) on a dated axis, laid out like SteamDB's charts: a plot with a
    crosshair and a hover box for the day under the pointer, faint bars for how many items each day has, range
    buttons (`toolbar`, which the overview puts beside the card's caption) and a navigator strip underneath
    that always shows every day, with a window over the part in view.

    Zoom by dragging across the plot, with the mouse wheel (Shift+wheel moves sideways), with the range buttons
    or by dragging the navigator's window or its edges; double-click the plot for every day again. The span in
    view is also the span every page counts: `view_changed` is sent whenever it, or the range button marked,
    changes, and `show_span` shows the span another chart (or the last session) chose.

    Self-contained: the overview card holds it today, and it fills a page of any width just as well.

    `view` is the span in view, [lo, hi) in epoch seconds, or None for every day, which widens as a running
    fetch adds days; a view that ends at the latest day keeps ending there as days are added.

    `notes` mark days the user picked out: each one's label sits on its point (a week or month's point shows
    the first in it), the hover box adds its description, and the navigator marks where it is. Clicking a day's
    dot (or its label) sends `note_requested` with the day, once the click can no longer turn out to be the
    first of a double-click; clicking a week or month's zooms in to its days."""

    note_requested = Signal(int)
    view_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(200)
        self.setMouseTracking(True)
        self.points: list[tuple[int, float, int]] = []
        self._days: list[int] = []  # the points' days, for finding the ones in view
        self._grouped: dict[tuple, tuple] = {}  # groups() by (lo, hi, width), for the points as they are now
        self.view: tuple[float, float] | None = None
        self.preset: str | None = "All"
        self._sent = (None, "All")  # the (view, preset) view_changed last told of
        a, b = self.domain()
        self._screen = (a, b, TIMELINE_BOUNDS[0])  # the (lo, hi, y bound) drawn now, eased towards _to
        self._from = self._to = self._screen
        self.zoom = motion.Tween(self._zoom_step, self)
        self._hover: tuple | None = None  # the (unit, start, end, mean, items) under the pointer
        self._pointer: QPointF | None = None  # where the pointer is over the chart
        self._drag: tuple | None = None  # (what, x where it started, view then)
        self._select: tuple[float, float] | None = None  # the band dragged across the plot, in x pixels
        self._pressed_at = 0  # when the left button last went down, in event milliseconds
        # A day's dot clicked: its note is asked for when a second click would no longer be a double-click,
        # as the note's dialog would take that click and the double-click (every day again) would open it.
        self._note_day = 0
        self._note = QTimer(self, singleShot=True)
        self._note.timeout.connect(lambda: self.note_requested.emit(self._note_day))
        self.notes: dict[int, tuple[str, str]] = {}  # {day start: (label, description)}
        self._note_days: list[int] = []
        self._pills: list[tuple[QRectF, tuple]] = []  # each note label as painted, and the point it is on
        self._version = 0  # counts changes to the points and notes, which the picture of the chart shows
        self._drawn: QPixmap | None = None
        self._drawn_for = None
        self._painted_for = None  # what the last paint showed
        self._new_data = False  # points or notes changed since the last paint: a change that then holds still

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
        self.points = list(points)
        self._version += 1
        self._new_data = True
        self._days = [d for d, _, _ in self.points]
        self._grouped.clear()
        if self.view is not None and self.view[1] >= old_end:
            grow = self.domain()[1] - old_end  # a fetch added later days: keep showing the latest ones
            self.view = (self.view[0] + grow, self.view[1] + grow)
        self._apply(False, follow=True)

    def set_notes(self, notes: dict[int, tuple[str, str]]) -> None:
        self._note.stop()  # another subreddit's notes: a dot just clicked was one of the last one's days
        self.notes = dict(notes)
        self._note_days = sorted(self.notes)
        self._version += 1
        self._new_data = True
        self.update()

    def notes_in(self, start: float, end: float) -> list[tuple[str, str]]:
        """The notes on days in [start, end), earliest first."""
        days = self._note_days
        return [self.notes[d] for d in days[bisect.bisect_left(days, start):bisect.bisect_left(days, end)]]

    def show_every_day(self) -> None:
        self.view, self.preset = None, "All"
        self._apply(False)

    def show_span(self, view: tuple[float, float] | None, preset: str | None = None) -> None:
        """Show a range button's span, else ``view`` (every day for None), at once."""
        if preset in self.presets and preset != "All":
            a, b = self.domain()
            self.set_view(b - dict(PRESETS)[preset] * DAY, b, preset=preset)
        elif view is not None:
            self.set_view(*view)
        else:
            self.show_every_day()

    def domain(self) -> tuple[float, float]:
        """Every day's span, from the first one's start to the end of the last one."""
        if not self._days:
            today = int(datetime.now(timezone.utc).timestamp()) // DAY * DAY
            return today, today + DAY
        return self._days[0], self._days[-1] + DAY

    def set_view(self, lo: float, hi: float, animate: bool = False, preset: str | None = None) -> None:
        """Show [lo, hi), kept inside the days there are and no narrower than MIN_SPAN."""
        self.view = self._fit(lo, hi, MIN_SPAN)
        self.preset = preset or ("All" if self.view is None else None)
        self._apply(animate)

    def _fit(self, lo: float, hi: float, least: float = 0.0) -> tuple[float, float] | None:
        """[lo, hi) kept inside the days there are and no narrower than ``least``; None when that's every day."""
        a, b = self.domain()
        span = min(max(hi - lo, least), b - a)
        lo = min(max(lo, a), b - span)
        return None if span >= b - a else (lo, lo + span)

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
            pt = QPointF(x_of(self._mid(start, end)), y_of(mean))
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
        if self.preset not in (None, "All"):  # a range button's span is always the latest days there are now
            b = self.domain()[1]
            self.view = self._fit(b - dict(PRESETS)[self.preset] * DAY, b)
        elif self.view is not None:  # the days may have changed under it
            self.view = self._fit(*self.view)
        lo, hi = self.view or self.domain()
        _, shown = self.groups(lo, hi, self._areas()[0].width())
        old, self._to = self._to, (lo, hi, timeline_bound([(g[0], g[2], g[3]) for g in shown]))
        self._sync_buttons()
        if follow and self.zoom.running():
            self._from = tuple(f + (b - a) for f, a, b in zip(self._from, old, self._to))
        elif animate and motion.enabled():
            self._from = self._screen
            self.zoom.value = 0.0
            self.zoom.set(1.0)
        else:
            self.zoom.stop()
            self._screen = self._to
            self.update()
        if self._pointer is not None:  # the box follows new means and a new grouping under a still pointer
            self._hover_at(self._pointer, self._areas()[0])
        if (self.view, self.preset) != self._sent:
            self._sent = (self.view, self.preset)
            self.view_changed.emit()

    def _zoom_step(self, f: float) -> None:
        self._screen = tuple(a + (b - a) * f for a, b in zip(self._from, self._to))
        self.update()

    def _sync_buttons(self) -> None:
        a, b = self.domain()
        for key, days in PRESETS:
            button = self.presets[key]
            button.setEnabled(days is None or days * DAY < b - a)  # a span longer than the days there are is All
            # A range button longer than the days there are shows every day: All is marked, though the button
            # is still the one kept (and saved), to show the latest days once there are more.
            button.setChecked(key == ("All" if self.view is None else self.preset))

    # -- geometry ---------------------------------------------------------------------------------------

    def _areas(self) -> tuple[QRectF, QRectF]:
        """The plot and the navigator under it, in widget coordinates."""
        fm = QFontMetricsF(_ui_font(9))
        left = fm.horizontalAdvance("−0.6") + 10
        bottom = self.height() - NAV_H - 1
        nav = QRectF(left, bottom, max(1.0, self.width() - left - 2), NAV_H)
        plot = QRectF(left, 8, nav.width(), max(1.0, bottom - NAV_GAP - fm.height() - 8 - 8))
        return plot, nav

    def _spot(self, point: tuple, plot: QRectF) -> QPointF:
        """Where a point (start, end, mean, items) is drawn now."""
        lo, hi, bound = self._screen
        start, end, mean = point[:3]
        return QPointF(self._x(self._mid(start, end), lo, hi, plot), plot.center().y() - mean / bound * plot.height() / 2)

    def _mid(self, start: float, end: float) -> float:
        """Where a point for [start, end) is drawn: the middle of the days of it there are, so the week or month
        at either end, which the days there are may only partly fill, sits inside the plot like the rest."""
        a, b = self.domain()
        return (max(start, a) + min(end, b)) / 2

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
        """The chart as it was last drawn for this span, size and data, kept as a picture, so hovering, a drag
        band, a crossfade or the window's tint repaint only what's on top: the band, the crosshair and its box."""
        colors, t = palette(), theme.tokens()
        dpr = self.devicePixelRatioF()
        lead = motion.lead(self)  # the picture starts on a whole device pixel, so it matches painting directly
        key = (self.size(), dpr, lead, self._screen, self._version, tuple(colors.values()), t["accent"], t["soft"])
        p = self.painter()
        if key == self._drawn_for:
            p.drawPixmap(-lead, self._drawn)
        # Painted like this once already, so it holds still: keep a picture. New points do too (a running fetch's,
        # four times a second): painted straight on, then again as a picture for the hover over it, it was painted
        # twice for each.
        elif key == self._painted_for or self._new_data:
            self._drawn = QPixmap((self.size() + QSize(lead.x(), lead.y())) * dpr)
            self._drawn.setDevicePixelRatio(dpr)
            self._drawn.fill(Qt.GlobalColor.transparent)
            under = QPainter(self._drawn)
            under.translate(lead)
            self._paint_chart(under, colors, t)
            under.end()
            self._drawn_for = key
            p.drawPixmap(-lead, self._drawn)
        else:  # each frame of a zoom or a drag differs: straight onto the widget
            self._paint_chart(p, colors, t)
            p.setFont(_ui_font(9))
        self._painted_for, self._new_data = key, False
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        plot, _ = self._areas()
        lo, hi, bound = self._screen
        if self._select:
            a, b = sorted(self._select)
            sel = QColor(t["accent"])
            sel.setAlpha(36)
            p.fillRect(QRectF(a, plot.top(), b - a, plot.height()), sel)
        elif self._hover:
            unit, start, end, mean, n = self._hover
            x = self._x(self._mid(start, end), lo, hi, plot)
            y = plot.center().y() - mean / bound * plot.height() / 2
            if plot.left() - 1 <= x <= plot.right() + 1:
                p.setPen(QPen(QColor(colors["muted"]), 1))
                p.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
                p.setPen(QPen(QColor(colors["surface"]), 2))
                p.setBrush(QColor(colors["line"]))
                p.drawEllipse(QPointF(x, y), 5.5, 5.5)
                p.setFont(_ui_font(9))
                self._paint_box(p, plot, x, self._hover)

    def _paint_chart(self, p: QPainter, colors: dict, t: dict) -> None:
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
            bars = []
            for start, end, _, n in shown:
                h = max(1.0, n / most * plot.height() * VOLUME)
                x0, x1 = x_of(start), x_of(end)
                bw = max(1.0, min((x1 - x0) * 0.6, 14))
                bars.append(QRectF((x0 + x1 - bw) / 2, plot.bottom() - h, bw, h))
            p.setRenderHint(QPainter.RenderHint.Antialiasing, False)  # square bars need none, and it's slow
            p.drawRects(bars)  # in one call: a call a bar took most of a frame for a few years of days
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._draw_line(p, shown, x_of, y_of, QPen(line, 2.25, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        ring = QPen(QColor(colors["surface"]), 1.5)
        if len(shown) <= DOTS:
            p.setPen(ring)
            p.setBrush(line)
            for start, end, mean, _ in shown:
                p.drawEllipse(QPointF(x_of(self._mid(start, end)), y_of(mean)), 4, 4)
        p.restore()
        self._paint_notes(p, plot, shown, ring)
        self._paint_navigator(p, nav, colors, t)

    def _paint_notes(self, p: QPainter, plot: QRectF, shown: list, ring: QPen) -> None:
        """Each noted point's dot, with the note's label in a pill above it (below it, near the top)."""
        t = theme.tokens()
        font = _ui_font(8)
        font.setWeight(QFont.Weight.DemiBold)
        fm = QFontMetricsF(font)
        p.setFont(font)
        self._pills = []
        for point in shown:
            marked = self.notes_in(point[0], point[1])
            if not marked:
                continue
            at = self._spot(point, plot)
            if not plot.left() - 1 <= at.x() <= plot.right() + 1:
                continue
            text = marked[0][0] + ("+" if len(marked) > 1 else "")
            h = fm.height() + 4
            w = max(h, fm.horizontalAdvance(text) + 10)
            top = at.y() - 9 - h if at.y() - 9 - h >= plot.top() - 4 else at.y() + 9
            pill = QRectF(at.x() - w / 2, top, w, h)
            p.setPen(ring)
            p.setBrush(QColor(t["accent"]))
            p.drawEllipse(at, 4, 4)
            p.drawRoundedRect(pill, h / 2, h / 2)
            p.setPen(QColor(t["on_accent"]))
            p.drawText(pill, int(Qt.AlignmentFlag.AlignCenter), text)
            self._pills.append((pill, point))
        p.setFont(_ui_font(9))

    def _paint_box(self, p: QPainter, plot: QRectF, x: float, hover: tuple) -> None:
        """The hover box: the day (or week, or month), its mean and how many items, beside the crosshair, and
        the description of each note in it."""
        unit, start, end, mean, n = hover
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
        # Notes: the label, then the description wrapped beside it.
        marked = self.notes_in(start, end)
        hfm, bfm = QFontMetricsF(head), QFontMetricsF(body)
        wrap = int(Qt.TextFlag.TextWordWrap)
        tag_w = max((hfm.horizontalAdvance(tag) for tag, _ in marked), default=0) + 8
        notes = [(tag, text, bfm.boundingRect(QRectF(0, 0, NOTE_WRAP, 1e5), wrap, text)) for tag, text in marked]
        if notes:
            text_w = max(r.width() for *_, r in notes)
            w = max(w, tag_w + text_w + 20)
            h += 13 + sum(max(r.height(), hfm.height()) + 4 for *_, r in notes) - 4
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
        if notes:
            top += 2
            p.setPen(QPen(QColor(*t["edge"]), 1))
            p.drawLine(QPointF(box.left() + 10, top), QPointF(box.right() - 10, top))
            top += 7
            for tag, text, r in notes:
                p.setFont(head)
                p.setPen(QColor(t["accent"]))
                p.drawText(QRectF(box.left() + 10, top, tag_w, hfm.height()),
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), tag)
                p.setFont(body)
                p.setPen(QColor(t["text"]))
                p.drawText(QRectF(box.left() + 10 + tag_w, top, w - 20 - tag_w, r.height()),
                           int(Qt.AlignmentFlag.AlignLeft) | wrap, text)
                top += max(r.height(), hfm.height()) + 4

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
        p.setPen(QPen(QColor(t["accent"]), 1.5))
        for d in self._note_days:
            if a <= d < b:
                x = self._x(d + DAY / 2, a, b, nav)
                p.drawLine(QPointF(x, nav.top() + 3), QPointF(x, nav.bottom() - 3))
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
        self._pressed_at = event.timestamp()
        self._note.stop()  # clicked again: the note waiting is for a double-click or a click that's moved on
        plot, nav = self._areas()
        pos = event.position()
        if nav.adjusted(-6, -4, 6, 4).contains(pos):
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
            part = self._nav_part(pos.x()) if nav.adjusted(-6, -4, 6, 4).contains(pos) else None
            if part is None and self._target(pos):
                part = "point"
            self.setCursor({"left": Qt.CursorShape.SizeHorCursor, "right": Qt.CursorShape.SizeHorCursor,
                            "move": Qt.CursorShape.OpenHandCursor,
                            "point": Qt.CursorShape.PointingHandCursor}.get(part, Qt.CursorShape.ArrowCursor))
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
        selected = self._select if self._drag and self._drag[0] == "select" else None
        self._drag = self._select = None
        self.unsetCursor()
        self.update()
        if not selected:
            return
        plot, _ = self._areas()
        x0, x1 = sorted(selected)
        lo, hi, _ = self._screen
        if x1 - x0 > 6:  # a drag, not a click: zoom to the days it covers
            self.set_view(self._t(x0, lo, hi, plot), self._t(x1, lo, hi, plot), animate=True)
        elif point := self._target(event.position()):
            if self.groups(lo, hi, plot.width())[0] == "day":
                # A double-click is timed from the first press, so a click held down waits that much less.
                self._note_day = int(point[0])
                wait = QApplication.styleHints().mouseDoubleClickInterval() - (event.timestamp() - self._pressed_at)
                self._note.start(max(0, wait))
            else:
                self.set_view(point[0], point[1], animate=True)

    def _target(self, pos: QPointF) -> tuple | None:
        """The point (start, end, mean, items) whose dot or note label is under pos."""
        hit = next((point for pill, point in self._pills if pill.contains(pos)), None)
        if hit is None:
            plot = self._areas()[0]
            self._hover_at(pos, plot)
            if self._hover:
                at = self._spot(self._hover[1:], plot)
                if (at.x() - pos.x()) ** 2 + (at.y() - pos.y()) ** 2 <= 12 ** 2:
                    hit = self._hover[1:]
        return hit

    def mouseDoubleClickEvent(self, event) -> None:
        self._note.stop()
        self._drag = self._select = None  # its second release is no click on a dot
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
            # The nearest point in view: one just past an edge would hide the crosshair there.
            inside = [g for g in shown if lo <= self._mid(g[0], g[1]) <= hi] or shown
            if inside:
                hover = (unit, *min(inside, key=lambda g: abs(self._mid(g[0], g[1]) - at)))
        if hover != self._hover:
            self._hover = hover
            self.update()

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            return True  # the hover box shows the day instead
        return super().event(e)


class NoteDialog(QDialog):
    """Add, change or remove the note on one day of the timeline: a label of up to two characters, shown on the
    chart, and a description, shown when the pointer is over that day. Sends `chosen` with the (label,
    description) saved, or two empty strings to remove the note; nothing if it's cancelled."""

    chosen = Signal(str, str)

    def __init__(self, day: int, tag: str = "", text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Note")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(6)
        when = datetime.fromtimestamp(day, timezone.utc)
        lay.addWidget(theme.mark(QLabel(f"{when:%A} {when.day} {when:%B %Y}"), "role", "heading"))
        lay.addSpacing(6)
        lay.addWidget(theme.mark(QLabel("Label (up to two characters)"), "role", "section"))
        self.tag = QLineEdit(tag)
        self.tag.setMaxLength(2)
        self.tag.setPlaceholderText("e.g. UP")
        lay.addWidget(self.tag)
        lay.addSpacing(4)
        lay.addWidget(theme.mark(QLabel("Description"), "role", "section"))
        self.text = QPlainTextEdit(text)
        self.text.setPlaceholderText("What happened on this day")
        self.text.setTabChangesFocus(True)
        self.text.moveCursor(QTextCursor.MoveOperation.End)  # changing a note usually adds to it
        self.text.setFixedHeight(self.text.fontMetrics().lineSpacing() * 4 + 16)
        lay.addWidget(self.text)
        lay.addSpacing(8)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        if tag or text:
            remove = QPushButton("Remove")
            remove.setAutoDefault(False)
            remove.clicked.connect(lambda: self._done("", ""))
            buttons.addWidget(remove)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.setAutoDefault(False)
        cancel.clicked.connect(self.reject)
        self.save = theme.mark(QPushButton("Save"), "primary", True)
        self.save.setDefault(True)
        self.save.clicked.connect(lambda: self._done(self.tag.text().strip(), self.text.toPlainText().strip()))
        self.tag.textChanged.connect(lambda: self.save.setEnabled(bool(self.tag.text().strip())))
        self.save.setEnabled(bool(tag.strip()))
        buttons.addWidget(cancel)
        buttons.addWidget(self.save)
        lay.addLayout(buttons)
        self.setMinimumWidth(QFontMetrics(self.font()).horizontalAdvance("x") * 52)
        self.tag.setFocus()

    def _done(self, tag: str, text: str) -> None:
        self.chosen.emit(tag, text)
        self.accept()
