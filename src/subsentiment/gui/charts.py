"""QtCharts views for the overview tab.

Colors follow the polarity job: blue = positive, red = negative, neutral gray
midpoint, with separate steps for light and dark surfaces. Red and gray sit
close together for protan viewers on dark, so every bar is also labelled by
category on the axis and by value, never by color alone.
"""
from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCharts import (
    QBarCategoryAxis, QBarSet, QChart, QChartView, QDateTimeAxis, QLineSeries,
    QScatterSeries, QStackedBarSeries, QValueAxis,
)
from PySide6.QtCore import QDateTime, QMargins, QPointF, Qt
from PySide6.QtGui import QColor, QCursor, QPainter, QPen
from PySide6.QtWidgets import QToolTip

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


class DistributionChart(QChartView):
    """One bar per sentiment label: count of items, labelled with count and share."""

    CATEGORIES = ("negative", "neutral", "positive")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setStyleSheet("background: transparent; border: none;")
        self.setBackgroundBrush(Qt.GlobalColor.transparent)
        self.setMinimumHeight(260)
        self.set_counts({"negative": 0, "neutral": 0, "positive": 0})

    def set_counts(self, counts: dict[str, int]) -> None:
        colors = palette()
        chart = _base_chart("Sentiment distribution", colors)
        total = sum(counts.values()) or 1
        # A stacked series where each set fills only its own category gives one
        # bar per category, each in its own color.
        series = QStackedBarSeries()
        series.setBarWidth(0.55)
        for i, cat in enumerate(self.CATEGORIES):
            s = QBarSet(cat.title())
            values = [0.0] * len(self.CATEGORIES)
            values[i] = float(counts.get(cat, 0))
            s.append(values)
            s.setColor(QColor(colors[cat]))
            s.setBorderColor(QColor(colors["surface"]))
            s.setLabelColor(QColor(colors["text"]))
            series.append(s)
        series.setLabelsVisible(True)
        series.setLabelsPosition(QStackedBarSeries.LabelsPosition.LabelsInsideEnd)
        series.setLabelsFormat("@value")
        series.hovered.connect(
            lambda state, index, barset: QToolTip.showText(
                QCursor.pos(),
                f"{self.CATEGORIES[index].title()}: {counts.get(self.CATEGORIES[index], 0)} "
                f"({100 * counts.get(self.CATEGORIES[index], 0) / total:.0f}%)") if state else QToolTip.hideText())
        chart.addSeries(series)

        x = QBarCategoryAxis()
        x.append([c.title() for c in self.CATEGORIES])
        _style_axis(x, colors)
        x.setGridLineVisible(False)
        y = QValueAxis()
        y.setRange(0, max(max(counts.values(), default=0) * 1.15, 1))
        y.setLabelFormat("%d")
        y.applyNiceNumbers()
        _style_axis(y, colors)
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(x)
        series.attachAxis(y)
        old = self.chart()
        self.setChart(chart)
        if old is not None:
            old.deleteLater()


class TimelineChart(QChartView):
    """Mean compound sentiment per day (UTC), on a fixed -1..1 scale."""

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
        pen.setWidthF(2)
        line.setPen(pen)
        dots = QScatterSeries()
        dots.setMarkerSize(9)
        dots.setColor(QColor(colors["line"]))
        ring = QPen(QColor(colors["surface"]))  # 2px surface ring so dots stand off the line
        ring.setWidthF(2)
        dots.setPen(ring)
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
        zero = QLineSeries()
        zpen = QPen(QColor(colors["muted"]))
        zpen.setWidthF(1)
        zpen.setStyle(Qt.PenStyle.DashLine)
        zero.setPen(zpen)
        chart.addSeries(zero)
        chart.addSeries(line)
        chart.addSeries(dots)

        x = QDateTimeAxis()
        x.setFormat("dd MMM")
        if points:
            lo, hi = points[0][0] * 1000, points[-1][0] * 1000
            if lo == hi:
                lo, hi = lo - 86_400_000, hi + 86_400_000
            pad = (hi - lo) * 0.03
            x.setRange(QDateTime.fromMSecsSinceEpoch(int(lo - pad), Qt.TimeSpec.UTC),
                       QDateTime.fromMSecsSinceEpoch(int(hi + pad), Qt.TimeSpec.UTC))
            zero.append(lo - pad, 0)
            zero.append(hi + pad, 0)
        x.setTickCount(6)
        _style_axis(x, colors)
        x.setGridLineVisible(False)
        y = QValueAxis()
        y.setRange(-1, 1)
        y.setTickCount(5)
        y.setLabelFormat("%+.1f")
        _style_axis(y, colors)
        chart.addAxis(x, Qt.AlignmentFlag.AlignBottom)
        chart.addAxis(y, Qt.AlignmentFlag.AlignLeft)
        for s in (zero, line, dots):
            s.attachAxis(x)
            s.attachAxis(y)
        old = self.chart()
        self.setChart(chart)
        if old is not None:
            old.deleteLater()
