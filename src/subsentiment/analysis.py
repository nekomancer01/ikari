"""What the overview shows about a set of scored posts and comments: counts, mean, shares and
daily means. Plain Python, so the GUI and tests share it."""
from __future__ import annotations


class Analysis:
    """Rows a running fetch adds are counted in with ``add`` as they come, so the overview doesn't go
    through every saved row again each second."""

    def __init__(self, rows=()):
        self.counts = {"negative": 0, "neutral": 0, "positive": 0}
        self.total = 0.0
        self.days: dict[int, list] = {}  # day start (UTC) -> [sum of scores, count]
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
