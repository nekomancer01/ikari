"""The overview tab's charts: the label split (painted here) and the daily timeline (QtCharts).

Colors follow the polarity job: blue = positive, orange = negative, neutral gray midpoint, with
separate steps for light and dark surfaces. Blue and orange stay apart for color-blind viewers, and
every bar is also labelled by category on the axis and by value, never by color alone.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCharts import QAreaSeries, QCategoryAxis, QChart, QChartView, QLineSeries, QScatterSeries
from PySide6.QtCore import QEvent, QMargins, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

from subsentiment.gui import motion, theme

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


def _base_chart() -> QChart:
    chart = QChart()
    chart.setBackgroundVisible(False)  # the card behind shows through; its caption names the chart
    chart.setPlotAreaBackgroundVisible(False)
    chart.setMargins(QMargins(4, 4, 4, 4))
    chart.legend().hide()
    chart.setAnimationOptions(QChart.AnimationOption.NoAnimation)
    return chart


def _style_axis(axis, colors: dict) -> None:
    axis.setLabelsColor(QColor(colors["muted"]))
    axis.setLinePenColor(QColor(colors["grid"]))
    axis.setGridLineColor(QColor(colors["grid"]))
    axis.setTitleBrush(QColor(colors["muted"]))
    axis.setLabelsFont(_ui_font(9))


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


# Symmetric y ranges for the timeline: the smallest that fits the data, so day-to-day
# movement is visible, with zero always in the middle so the sign reads at a glance.
TIMELINE_BOUNDS = (0.2, 0.4, 0.6, 1.0)
# Day steps for the date axis, smallest first, so at most about seven dates show.
DAY_STEPS = (1, 2, 3, 7, 14, 30, 61, 91, 182, 365)
NEUTRAL_BAND = 0.05  # matches the label thresholds in sentiment.py


def timeline_bound(points: list[tuple[int, float, int]]) -> float:
    peak = max((abs(mean) for _, mean, _ in points), default=0.0)
    return next((b for b in TIMELINE_BOUNDS if peak * 1.1 <= b), TIMELINE_BOUNDS[-1])


def _date_label(day: int) -> str:
    d = datetime.fromtimestamp(day, tz=timezone.utc)
    return f"{d.day} {d:%b}"


def _signed(v: float) -> str:
    return "0" if abs(v) < 1e-9 else f"{v:+.1f}".replace("-", "\u2212")


class TimelineChart(QChartView):
    """Mean compound sentiment per day (UTC), on a symmetric scale centred on zero."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setStyleSheet("background: transparent; border: none;")
        self.setBackgroundBrush(Qt.GlobalColor.transparent)
        self.setMinimumHeight(200)
        self.set_points([])

    def set_points(self, points: list[tuple[int, float, int]]) -> None:
        colors = palette()
        if (points, colors) == getattr(self, "_shown", None):
            return  # nothing new (a running fetch asks once a second)
        if getattr(self, "_shown", None) is not None and colors == self._shown[1]:
            motion.crossfade(self)  # new days or new means: the old line fades into the new one
        self._shown = (list(points), colors)
        chart = _base_chart()
        pen = QPen(QColor(colors["line"]))
        pen.setWidthF(2.25)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        lines: list[QLineSeries] = []  # one per run of consecutive days, so a day with no data shows as a gap
        dots = QScatterSeries()
        dots.setMarkerSize(8)
        dots.setColor(QColor(colors["line"]))
        ring = QPen(QColor(colors["surface"]))  # surface ring so dots stand off the line
        ring.setWidthF(1.5)
        dots.setPen(ring)
        dots.setVisible(len(points) <= 45)  # past that, dots only clutter the line
        by_ms: dict[int, tuple[float, int]] = {}
        prev = None
        for day, mean, n in points:
            if prev is None or day - prev > 86400:
                lines.append(QLineSeries())
                lines[-1].setPen(pen)
            prev = day
            ms = day * 1000
            lines[-1].append(ms, mean)
            dots.append(ms, mean)
            by_ms[ms] = (mean, n)

        def on_hover(point: QPointF, state: bool) -> None:
            if not state:
                QToolTip.hideText()
                return
            mean, n = by_ms.get(int(round(point.x())), (point.y(), 0))
            day = datetime.fromtimestamp(point.x() / 1000, tz=timezone.utc).strftime("%a %d %b %Y")
            QToolTip.showText(QCursor.pos(), f"{day}\nmean {mean:+.3f} across {n} items")

        dots.hovered.connect(on_hover)

        if points:
            first, last = points[0][0], points[-1][0]
        else:
            first = int(datetime.now(timezone.utc).timestamp()) // 86400 * 86400
            last = first
        if first == last:
            first, last = first - 86400, last + 86400
        pad = max((last - first) * 0.04, 3600)
        lo, hi = (first - pad) * 1000, (last + pad) * 1000

        # A faint band marks the neutral zone, so days that read as neutral are obvious.
        band = QAreaSeries()
        band_hi, band_lo = QLineSeries(band), QLineSeries(band)  # the area series doesn't own its edges
        for xv in (lo, hi):
            band_hi.append(xv, NEUTRAL_BAND)
            band_lo.append(xv, -NEUTRAL_BAND)
        band.setUpperSeries(band_hi)
        band.setLowerSeries(band_lo)
        fill = QColor(colors["neutral"])
        fill.setAlpha(30)
        band.setBrush(QBrush(fill))
        band.setPen(QPen(Qt.PenStyle.NoPen))
        zero = QLineSeries()
        zpen = QPen(QColor(colors["muted"]))
        zpen.setWidthF(1)
        zpen.setStyle(Qt.PenStyle.DashLine)
        zero.setPen(zpen)
        zero.append(lo, 0)
        zero.append(hi, 0)
        chart.addSeries(band)
        chart.addSeries(zero)
        for line in lines:
            chart.addSeries(line)
        chart.addSeries(dots)

        # Dates sit exactly on day boundaries, stepped so they never crowd.
        x = QCategoryAxis()
        x.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        x.setRange(lo, hi)
        x.setStartValue(lo)
        span_days = (last - first) // 86400
        step = next((s for s in DAY_STEPS if span_days / s <= 6), DAY_STEPS[-1])
        for day in range(first, last + 1, step * 86400):
            x.append(_date_label(day), day * 1000)
        _style_axis(x, colors)
        x.setGridLineVisible(False)

        bound = timeline_bound(points)
        y = QCategoryAxis()
        y.setLabelsPosition(QCategoryAxis.AxisLabelsPosition.AxisLabelsPositionOnValue)
        y.setRange(-bound, bound)
        y.setStartValue(-bound - 1e-6)
        for v in (-bound, -bound / 2, 0.0, bound / 2, bound):
            y.append(_signed(v), v)
        _style_axis(y, colors)
        y.setLineVisible(False)
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y, Qt.AlignmentFlag.AlignLeft)
        for s in (band, zero, *lines, dots):
            s.attachAxis(x)
            s.attachAxis(y)
        old = self.chart()
        self.setChart(chart)
        if old is not None:
            old.deleteLater()
