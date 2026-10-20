"""What the overview shows about a set of scored posts and comments: counts, mean, shares, daily means
and the strongest items. Plain Python, so the GUI and tests share it."""
from __future__ import annotations

import heapq
from operator import itemgetter

_score = itemgetter("sentiment")


class Analysis:
    """Rows a running fetch adds are counted in with ``add`` as they come, so the overview doesn't go
    through every saved row again each second. Only the strongest ``STRONGEST`` items are kept."""

    STRONGEST = 10

    def __init__(self, rows=()):
        self.counts = {"negative": 0, "neutral": 0, "positive": 0}
        self.total = 0.0
        self.days: dict[int, list] = {}  # day start (UTC) -> [sum of scores, count]
        self.top: list = []  # most positive first; ties latest first, as the tail of a sorted list had them
        self.bottom: list = []  # most negative first; ties earliest first
        self.add(rows)

    def add(self, rows) -> None:
        rows = list(rows)
        counts, days = self.counts, self.days
        for r in rows:
            s = r["sentiment"]
            counts[r["label"]] += 1
            self.total += s
            bucket = days.setdefault(int(r["created_utc"] // 86400 * 86400), [0.0, 0])
            bucket[0] += s
            bucket[1] += 1
        self.top = heapq.nlargest(self.STRONGEST, [*reversed(rows), *self.top], key=_score)
        self.bottom = heapq.nsmallest(self.STRONGEST, self.bottom + rows, key=_score)

    @classmethod
    def combined(cls, *parts: Analysis) -> Analysis:
        """One analysis of every row in ``parts``, as if their rows were added in that order."""
        out = cls()
        for a in parts:
            for k, n in a.counts.items():
                out.counts[k] += n
            out.total += a.total
            for day, (total, n) in a.days.items():
                bucket = out.days.setdefault(day, [0.0, 0])
                bucket[0] += total
                bucket[1] += n
        out.top = heapq.nlargest(cls.STRONGEST, [r for a in reversed(parts) for r in a.top], key=_score)
        out.bottom = heapq.nsmallest(cls.STRONGEST, [r for a in parts for r in a.bottom], key=_score)
        return out

    @property
    def count(self) -> int:
        return sum(self.counts.values())

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def daily_means(self) -> list[tuple[int, float, int]]:
        """[(day_start_utc, mean_sentiment, n)] sorted by day."""
        return [(d, total / n, n) for d, (total, n) in sorted(self.days.items())]

    def share(self, label: str) -> float:
        return self.counts[label] / self.count if self.count else 0.0

    def most_positive(self, n: int = STRONGEST) -> list:
        return self.top[:n]

    def most_negative(self, n: int = STRONGEST) -> list:
        return self.bottom[:n]
