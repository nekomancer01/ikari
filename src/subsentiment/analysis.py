"""What the overview shows about a set of scored posts and comments: counts, mean, shares, daily means
and the strongest items. Plain Python, so the GUI and tests share it."""
from __future__ import annotations

import heapq


class Analysis:
    def __init__(self, rows):
        self.rows = list(rows)
        self.counts = {"negative": 0, "neutral": 0, "positive": 0}
        total = 0.0
        buckets: dict[int, list[float]] = {}
        for r in self.rows:
            self.counts[r["label"]] += 1
            total += r["sentiment"]
            buckets.setdefault(int(r["created_utc"] // 86400 * 86400), []).append(r["sentiment"])
        self.count = len(self.rows)
        self.mean = total / self.count if self.count else 0.0
        # [(day_start_utc, mean_sentiment, n)] sorted by day.
        self.daily_means = [(d, sum(v) / len(v), len(v)) for d, v in sorted(buckets.items())]

    def share(self, label: str) -> float:
        return self.counts[label] / self.count if self.count else 0.0

    def most_positive(self, n: int = 10) -> list:
        # Reversed so ties come latest first, as they did when this was the tail of a sorted list.
        return heapq.nlargest(n, reversed(self.rows), key=lambda r: r["sentiment"])

    def most_negative(self, n: int = 10) -> list:
        return heapq.nsmallest(n, self.rows, key=lambda r: r["sentiment"])
