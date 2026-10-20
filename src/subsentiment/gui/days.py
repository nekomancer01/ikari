"""The span of saved days every page counts, as chosen on the timeline.

The timeline's range buttons, zoom, drag selection and navigator choose a span of days; the Overview's tiles
and distribution, the Posts and Comments tables and CSV export then count only the days in it. Nothing is
fetched. Days are UTC, like the timeline chart and Download coverage.

The span is remembered per subreddit: a range button as its name, so "the latest month" keeps meaning the
latest month as days are fetched, and any other span as its two ends. Every day (the default) is stored as no
choice at all. Days chosen with the Days picker of earlier versions are read as the span from the first to the
last of them.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton

from subsentiment.gui import theme

DAY = 86400
WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
SETTINGS_GROUP = "analysis_days"  # earlier versions' picked days: analysis_days/<subreddit, lowercased> = "2026-10-17,2026-10-18"
RANGE_GROUP = "timeline_range"  # timeline_range/<subreddit, lowercased> = "1m", or "<lo>,<hi>" in epoch seconds


def day_of(ts: float) -> int:
    """Start of the UTC day ``ts`` falls in, in epoch seconds."""
    return int(ts // DAY * DAY)


def as_date(day: int) -> date:
    return datetime.fromtimestamp(day, timezone.utc).date()


def from_date(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def whole(span: tuple[float, float] | None) -> tuple[int, int] | None:
    """The days a span on the timeline counts, as [first day's start, end): those whose point (drawn at
    midday) is in it, so a day only partly in view at an edge isn't counted. None (every day) stays None."""
    if span is None:
        return None
    return tuple(math.ceil((t - DAY / 2) / DAY) * DAY for t in span)


def keep(rows, days: tuple[int, int] | None) -> list:
    """The rows created in ``days`` (from `whole`); all of them for None. Always a new list: the tables extend
    the list they were given with a running fetch's rows."""
    if days is None:
        return list(rows)
    first, end = days
    return [r for r in rows if r["created_utc"] is not None and first <= r["created_utc"] < end]


def settings_keys(subreddit: str) -> tuple[str, str]:
    return f"{RANGE_GROUP}/{subreddit.lower()}", f"{SETTINGS_GROUP}/{subreddit.lower()}"


def load_range(settings, subreddit: str) -> tuple[tuple[float, float] | None, str | None]:
    """The span remembered for a subreddit, as (span, range button): one of them, or neither for every day."""
    key, old = settings_keys(subreddit or "")
    if not subreddit:
        return None, None
    if settings.contains(key):
        text = settings.value(key, "", str)
        try:
            lo, hi = (float(v) for v in text.split(","))
            return ((lo, hi), None) if lo < hi else (None, None)
        except ValueError:
            return None, text or None
    picked = set()
    for part in settings.value(old, "", str).split(",") if settings.contains(old) else ():
        try:
            picked.add(from_date(date.fromisoformat(part.strip())))
        except ValueError:
            continue
    return ((min(picked), max(picked) + DAY), None) if picked else (None, None)


def save_range(settings, subreddit: str, span: tuple[float, float] | None, preset: str | None) -> None:
    """Remember a range button, else a span; every day is stored as no choice at all."""
    key, old = settings_keys(subreddit)
    settings.remove(old)
    if preset not in (None, "All"):
        settings.setValue(key, preset)
    elif span is not None:
        settings.setValue(key, f"{span[0]:.0f},{span[1]:.0f}")
    else:
        settings.remove(key)


def short_date(day: int, year: bool = False) -> str:
    d = as_date(day)
    return f"{d.day} {d:%b %Y}" if year else f"{d.day} {d:%b}"


def flat_button(text: str) -> QPushButton:
    b = theme.mark(theme.mark(QPushButton(text), "flat", True), "small", True)
    b.setAutoDefault(False)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b
