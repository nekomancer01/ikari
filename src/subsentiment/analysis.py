"""What the overview shows about a set of scored posts and comments: counts, mean, shares and
daily means. Plain Python, so the GUI and tests share it."""
from __future__ import annotations

LABELS = ("negative", "neutral", "positive")


class Analysis:
    """Rows a running fetch adds are counted in with ``add`` as they come, so the overview doesn't go
    through every saved row again each second. Each day keeps its own totals, so ``between`` can count any
    run of days (the span the timeline shows) without going through the rows again."""

    def __init__(self, rows=()):
        self.counts = dict.fromkeys(LABELS, 0)
        self.total = 0.0
        self.days: dict[int, list] = {}  # day start (UTC) -> [sum of scores, count, {label: count}]
        self.add(rows)

    def add(self, rows) -> None:
        counts, days = self.counts, self.days
        for r in rows:
            if r["created_utc"] is None:  # no day to count it on
                continue
            s = r["sentiment"]
            counts[r["label"]] += 1
            self.total += s
            bucket = days.get(day := int(r["created_utc"] // 86400 * 86400))
            if bucket is None:
                bucket = days[day] = [0.0, 0, dict.fromkeys(LABELS, 0)]
            bucket[0] += s
            bucket[1] += 1
            bucket[2][r["label"]] += 1

    @classmethod
    def combined(cls, *parts: Analysis, days: tuple[int, int] | None = None) -> Analysis:
        """One analysis of every row in ``parts``, as if their rows were added in that order; with ``days``
        ([first day's start, end)), of only the days in it."""
        out = cls()
        for a in parts:
            for day, (total, n, labels) in a.days.items():
                if days is not None and not days[0] <= day < days[1]:
                    continue
                bucket = out.days.setdefault(day, [0.0, 0, dict.fromkeys(LABELS, 0)])
                bucket[0] += total
                bucket[1] += n
                out.total += total
                for k, m in labels.items():
                    bucket[2][k] += m
                    out.counts[k] += m
        return out

    def between(self, days: tuple[int, int] | None) -> Analysis:
        """The days in [first day's start, end), or every day for None."""
        return self if days is None else Analysis.combined(self, days=days)

    @property
    def count(self) -> int:
        return sum(self.counts.values())

    @property
    def mean(self) -> float:
        return self.total / self.count if self.count else 0.0

    @property
    def daily_means(self) -> list[tuple[int, float, int]]:
        """[(day_start_utc, mean_sentiment, n)] sorted by day."""
        return [(d, total / n, n) for d, (total, n, _) in sorted(self.days.items())]

    def share(self, label: str) -> float:
        return self.counts[label] / self.count if self.count else 0.0
