"""What the overview shows about a set of scored posts and comments: counts, mean, shares and
daily means, and the words they use most. Plain Python, so the GUI and tests share it."""
from __future__ import annotations

import heapq
import re
import sys
from collections import Counter
from itertools import chain

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

    def add(self, rows, sign: int = 1) -> None:
        """Count ``rows`` in, or with ``sign`` -1 take rows counted before back out (a rescored row is taken out
        as it was and counted in as it is)."""
        counts, days = self.counts, self.days
        for r in rows:
            if r["created_utc"] is None:  # no day to count it on
                continue
            s = r["sentiment"] * sign
            counts[r["label"]] += sign
            self.total += s
            bucket = days.get(day := int(r["created_utc"] // 86400 * 86400))
            if bucket is None:
                bucket = days[day] = [0.0, 0, dict.fromkeys(LABELS, 0)]
            bucket[0] += s
            bucket[1] += sign
            bucket[2][r["label"]] += sign
            if not bucket[1]:  # every row of the day taken out
                del days[day]

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


# Words too common to say anything about a subreddit, and Reddit's own markup and placeholders.
STOPWORDS = frozenset("""
a about above after again against all almost also although always am among an and another any anyone anything
are aren't around as at away back be because been before being below between both but by can can't cannot
could couldn't did didn't do does doesn't doing don't done down during each either else enough even ever every
everyone everything few for from further get gets getting go goes going gone got gotten had hadn't has hasn't
have haven't having he he'd he'll he's her here here's hers herself him himself his how how's however i i'd
i'll i'm i've if in into is isn't it it'd it'll it's its itself just let let's like made make makes many may
maybe me might mine more most much must mustn't my myself need needs never no nor not nothing now of off often
oh ok okay on once one only or other others otherwise our ours ourselves out over own per probably put quite
rather really same say says said see seem seems she she'd she'll she's should shouldn't since so some someone
something still such sure take than that that's the their theirs them themselves then there there's these they
they'd they'll they're they've thing things think this those though through thus to too took toward under until
up upon us use used using very want was wasn't way we we'd we'll we're we've well were weren't what what's
when when's where where's whether which while who who's whom whose why why's will with within without won't
would wouldn't yeah yes yet you you'd you'll you're you've your yours yourself yourselves
im ive id dont doesnt didnt cant wont isnt arent wasnt thats theres whats youre theyre lol lmao gonna wanna
http https www com amp gt lt nbsp removed deleted edit png jpg jpeg gif imgur reddit preview redd
""".split())
_TEXT = {"posts": ("title", "selftext"), "comments": ("body",)}
# Everything but letters, digits and apostrophes splits words (through general punctuation, so dashes too).
_SPACES = {i: " " for i in range(0x2070) if not chr(i).isalnum() and chr(i) != "'"} | {0x2019: "'"}  # ’ too
_LINK = re.compile(r"\S*(?:https?://|www\.)\S*|&\w+;")


def words_in(text: str) -> set[str]:
    """The distinct words in a post or comment worth putting in a word cloud: lowercased, without links,
    markup, numbers, stopwords or words under three letters."""
    text = text.lower()
    if "http" in text or "www." in text or "&" in text:
        text = _LINK.sub(" ", text)
    words = set(text.translate(_SPACES).split())
    if "'" in text:
        words = {w.strip("'").removesuffix("'s") for w in words}
    return {w for w in words - STOPWORDS if len(w) > 2 and not w.isdigit()}


class WordCounts:
    """How many posts or comments use each word, and how many of those are positive and negative, so the cloud
    can size a word by how often it comes up and colour it by which way the items using it lean.

    Rows are kept by id: a fetch that rescores or edits a saved row replaces what that row counted. ``seen``
    ({id: (text, words)}) can be shared between counts of the same rows, so each text is split into words
    once, not again every time another span of days is counted."""

    def __init__(self, kind: str, rows=(), seen: dict | None = None):
        self.fields = _TEXT[kind]
        self.counts = {"all": Counter(), "positive": Counter(), "negative": Counter()}
        self._rows: dict[str, tuple[str, tuple[str, ...]]] = {}
        self.seen = {} if seen is None else seen
        self.add(rows)

    def add(self, rows) -> None:
        counts, seen, fields, kept = self.counts, self.seen, self.fields, self._rows
        added = {"all": [], "positive": [], "negative": []}  # counted in at the end, in one go each
        for r in rows:
            text = (r[fields[0]] or "") if len(fields) == 1 else " ".join(r[f] or "" for f in fields)
            hit = seen.get(r["id"])
            if hit is not None and hit[0] == text:
                words = hit[1]
            else:
                words = tuple(map(sys.intern, words_in(text)))  # a word's text is kept once, however many use it
                seen[r["id"]] = (text, words)
            old = kept.get(r["id"])
            if old is not None:
                counts["all"].subtract(old[1])
                if old[0] in counts:
                    counts[old[0]].subtract(old[1])
            label = r["label"]
            kept[r["id"]] = (label, words)
            added["all"].append(words)
            if label in added:
                added[label].append(words)
        for key, lists in added.items():
            counts[key].update(chain.from_iterable(lists))

    def remove(self, ids) -> None:
        """Stop counting the rows with these ids (ones never counted are skipped)."""
        gone = {"all": [], "positive": [], "negative": []}
        for i in ids:
            row = self._rows.pop(i, None)
            if row is not None:
                gone["all"].append(row[1])
                if row[0] in gone:
                    gone[row[0]].append(row[1])
        for key, lists in gone.items():
            if lists:
                self.counts[key].subtract(Counter(chain.from_iterable(lists)))

    def ids(self):
        return self._rows.keys()

    @staticmethod
    def top(*parts: WordCounts, n: int = 100) -> list[tuple[str, int, float]]:
        """The ``n`` words used by the most items across ``parts``: [(word, items, lean)], where lean runs
        from -1 (every item using it negative) to +1 (every one positive)."""
        total = parts[0].counts["all"] if len(parts) == 1 else sum((p.counts["all"] for p in parts), Counter())
        out = []
        # Ties go alphabetically, so the same rows always make the same cloud.
        for w, k in heapq.nsmallest(n, total.items(), key=lambda wk: (-wk[1], wk[0])):
            if k > 0:  # a replaced row can leave a word at zero
                lean = sum(p.counts["positive"][w] - p.counts["negative"][w] for p in parts) / k
                out.append((w, k, lean))
        return out
