"""QtCharts views for the overview tab.

Colors follow the polarity job: blue = positive, red = negative, neutral gray
midpoint, with separate steps for light and dark surfaces. Red and gray sit
close together for protan viewers on dark, so every bar is also labelled by
category on the axis and by value, never by color alone.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCharts import (
    QAreaSeries, QBarCategoryAxis, QBarSet, QCategoryAxis, QChart, QChartView, QLineSeries,
    QScatterSeries, QStackedBarSeries, QValueAxis,
)
from PySide6.QtCore import QMargins, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QCursor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication, QGraphicsPathItem, QGraphicsSimpleTextItem, QToolTip

from subsentiment.gui import theme

LIGHT = {
    "surface": "#fcfcfb", "text": "#0b0b0b", "muted": "#52514e", "grid": "#e4e3df",
    "positive": "#2a78d6", "neutral": "#8a8984", "negative": "#e34948", "line": "#2a78d6",
}
DARK = {
    "surface": "#1a1a19", "text": "#f0efec", "muted": "#c3c2b7", "grid": "#383835",
    "positive": "#3987e5", "neutral": "#8a8984", "negative": "#e66767", "line": "#3987e5",
}


# Approximate color of the glass card behind the charts, for the dots' surface ring.
CARD = {"light": "#f3f5fc", "dark": "#1b1f2e"}


def palette() -> dict:
    """Validated series colors for the current theme, with text and grid from the theme."""
    t = theme.tokens()
    mode = theme.mode()
    grid = QColor(*t["edge"][:3])
    grid.setAlpha(min(255, t["edge"][3] * 2))
    return dict(DARK if mode == "dark" else LIGHT, surface=CARD[mode], text=t["text"], muted=t["muted"],
                grid=grid.name(QColor.NameFormat.HexArgb))


def _ui_font(point_size: float) -> QFont:
    """The app's UI font (Manrope, tabular figures) at a chart-label size."""
    f = QFont(QApplication.font())
    f.setPointSizeF(point_size)
    return f


def _base_chart(title: str, colors: dict) -> QChart:
    chart = QChart()
    chart.setTitle(title)
    chart.setBackgroundVisible(False)  # the glass card behind shows through
    chart.setPlotAreaBackgroundVisible(False)
    chart.setTitleBrush(QColor(colors["text"]))
    chart.setTitleFont(theme.display_font(11.5, 600))
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


class DistributionChart(QChartView):
    """One bar per sentiment label: count of items, labelled above with count and share."""

    CATEGORIES = ("negative", "neutral", "positive")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setStyleSheet("background: transparent; border: none;")
        self.setBackgroundBrush(Qt.GlobalColor.transparent)
        self.setMinimumHeight(260)
        self._labels: list[tuple[QGraphicsSimpleTextItem, QGraphicsSimpleTextItem, int, float]] = []
        self._bars: list[tuple[QGraphicsPathItem, int, float]] = []
        self._series = None
        self.set_counts({"negative": 0, "neutral": 0, "positive": 0})

    def set_counts(self, counts: dict[str, int]) -> None:
        colors = palette()
        chart = _base_chart("Sentiment distribution", colors)
        total = sum(counts.values())
        # A stacked series where each set fills only its own category gives one
        # bar per category, each in its own color, centred on its axis label.
        series = QStackedBarSeries()
        series.setBarWidth(0.62)
        for i, cat in enumerate(self.CATEGORIES):
            s = QBarSet(cat.title())
            values = [0.0] * len(self.CATEGORIES)
            values[i] = float(counts.get(cat, 0))
            s.append(values)
            # QtCharts can't round bars, so its own bars are invisible (they still drive
            # layout and hover) and rounded shapes are drawn over them in _place_labels.
            s.setColor(Qt.GlobalColor.transparent)
            s.setBorderColor(Qt.GlobalColor.transparent)
            series.append(s)
        series.hovered.connect(
            lambda state, index, barset: QToolTip.showText(
                QCursor.pos(),
                f"{self.CATEGORIES[index].title()}: {counts.get(self.CATEGORIES[index], 0):,} "
                f"({100 * counts.get(self.CATEGORIES[index], 0) / (total or 1):.0f}%)") if state else QToolTip.hideText())
        chart.addSeries(series)

        x = QBarCategoryAxis()
        x.append([c.title() for c in self.CATEGORIES])
        _style_axis(x, colors)
        x.setGridLineVisible(False)
        x.setLabelsColor(QColor(colors["text"]))
        # Every bar carries its own count and share, so the value axis only sets the
        # scale: no labels or grid competing with the bars. Headroom for the labels.
        y = QValueAxis()
        y.setRange(0, max(max(counts.values(), default=0) * 1.16, 1))
        y.setVisible(False)
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(x)
        series.attachAxis(y)

        # Value labels sit above each bar in the text color, so they stay readable on
        # every bar color in both themes (QtCharts can't put stacked labels outside).
        self._bars = []
        for i, cat in enumerate(self.CATEGORIES):
            bar = QGraphicsPathItem(chart)
            bar.setBrush(QColor(colors[cat]))
            bar.setPen(QPen(Qt.PenStyle.NoPen))
            bar.setAcceptHoverEvents(False)  # hover goes to the series' own bar underneath
            bar.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
            self._bars.append((bar, i, float(counts.get(cat, 0))))
        self._labels = []
        count_font, share_font = theme.display_font(10.5, 600), _ui_font(9)
        for i, cat in enumerate(self.CATEGORIES):
            n = counts.get(cat, 0)
            count = QGraphicsSimpleTextItem(f"{n:,}", chart)
            count.setFont(count_font)
            count.setBrush(QColor(colors["text"]))
            share = QGraphicsSimpleTextItem(f"{100 * n / total:.0f}%" if total else "", chart)
            share.setFont(share_font)
            share.setBrush(QColor(colors["muted"]))
            self._labels.append((count, share, i, float(n)))
        self._series = series
        chart.plotAreaChanged.connect(lambda _area: self._place_labels())

        old = self.chart()
        self.setChart(chart)
        if old is not None:
            old.deleteLater()
        self._place_labels()

    def _place_labels(self) -> None:
        chart = self.chart()
        if chart is None or self._series is None:
            return
        half = self._series.barWidth() / 2
        for bar, i, value in self._bars:
            top_left = chart.mapToPosition(QPointF(i - half, value), self._series)
            base_right = chart.mapToPosition(QPointF(i + half, 0), self._series)
            bar.setPath(_rounded_bar(QRectF(top_left, base_right).normalized()))
        for count, share, i, value in self._labels:
            top = chart.mapToPosition(QPointF(i, value), self._series)
            c, sh = count.boundingRect(), share.boundingRect()
            gap = 5.0 if sh.width() else 0.0
            left = top.x() - (c.width() + gap + sh.width()) / 2
            base = top.y() - 4  # count and share share a baseline just above the bar
            count.setPos(left, base - c.height())
            share.setPos(left + c.width() + gap, base - c.height() + (c.height() - sh.height()) * 0.72)


BAR_RADIUS = 9.0  # px at the top corners; the base stays square on the axis


def _rounded_bar(r: QRectF) -> QPainterPath:
    """A bar with rounded top corners, the radius shrinking for short or narrow bars."""
    path = QPainterPath()
    if r.height() <= 0 or r.width() <= 0:
        return path
    rad = min(BAR_RADIUS, r.width() / 2, r.height())
    path.moveTo(r.left(), r.bottom())
    path.lineTo(r.left(), r.top() + rad)
    path.arcTo(QRectF(r.left(), r.top(), 2 * rad, 2 * rad), 180, -90)
    path.lineTo(r.right() - rad, r.top())
    path.arcTo(QRectF(r.right() - 2 * rad, r.top(), 2 * rad, 2 * rad), 90, -90)
    path.lineTo(r.right(), r.bottom())
    path.closeSubpath()
    return path


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
        self.setMinimumHeight(260)
        self.set_points([])

    def set_points(self, points: list[tuple[int, float, int]]) -> None:
        colors = palette()
        chart = _base_chart("Mean sentiment per day (UTC)", colors)
        line = QLineSeries()
        pen = QPen(QColor(colors["line"]))
        pen.setWidthF(2.25)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        line.setPen(pen)
        dots = QScatterSeries()
        dots.setMarkerSize(8)
        dots.setColor(QColor(colors["line"]))
        ring = QPen(QColor(colors["surface"]))  # surface ring so dots stand off the line
        ring.setWidthF(1.5)
        dots.setPen(ring)
        dots.setVisible(len(points) <= 45)  # past that, dots only clutter the line
        by_ms: dict[int, tuple[float, int]] = {}
        for day, mean, n in points:
            ms = day * 1000
            line.append(ms, mean)
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
        for s in (band, zero, line, dots):
            s.attachAxis(x)
            s.attachAxis(y)
        old = self.chart()
        self.setChart(chart)
        if old is not None:
            old.deleteLater()
