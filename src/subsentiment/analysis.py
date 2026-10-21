"""What the overview shows about a set of scored posts and comments: counts, mean, shares and
daily means, and the words they use most. Plain Python, so the GUI and tests share it."""
from __future__ import annotations

import heapq
import re
import sys
from collections import Counter, namedtuple
from itertools import chain
from operator import itemgetter

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


_Counted = namedtuple("_Counted", "label words")  # what a row that may change later counted


class WordCounts:
    """How many posts or comments use each word, and how many of those are positive and negative, so the cloud
    can size a word by how often it comes up and colour it by which way the items using it lean.

    Rows are kept by id: a fetch that rescores or edits a saved row replaces what that row counted. A row read
    for the word cloud brings its words along (store.Row.words), split once as it was read; any other row's text
    is split here."""

    def __init__(self, kind: str, rows=()):
        self.fields = _TEXT[kind]
        self.counts = {"all": Counter(), "positive": Counter(), "negative": Counter()}
        self._rows: dict[str, object] = {}  # id -> the row as counted (its label and words)
        self.add(rows)

    def words(self, row) -> tuple[str, ...]:
        words = getattr(row, "words", None)
        if words is not None:
            return words
        fields = self.fields
        text = (row[fields[0]] or "") if len(fields) == 1 else " ".join(row[f] or "" for f in fields)
        return tuple(map(sys.intern, words_in(text)))  # a word's text is kept once, however many use it

    def add(self, rows) -> None:
        counts, kept, words = self.counts, self._rows, self.words
        added = {"all": [], "positive": [], "negative": []}  # counted in at the end, in one go each
        gone = {"all": [], "positive": [], "negative": []}  # what rows counted before, saved again, counted
        for r in rows:
            try:  # a saved row (store.Row): read as attributes, much quicker than by name
                rid, label, w = r.id, r.label, r.words
            except AttributeError:
                rid, label, w = r["id"], r["label"], None
            old = kept.get(rid)
            if old is not None:
                gone["all"].append(old.words)
                if old.label in gone:
                    gone[old.label].append(old.words)
            if w is None:  # split here; what was counted is kept with it, as the row may change after
                w = words(r)
                kept[rid] = _Counted(label, w)
            else:  # a saved row doesn't change, so it's kept as it is
                kept[rid] = r
            added["all"].append(w)
            if label in added:
                added[label].append(w)
        for key, lists in added.items():
            counts[key].update(chain.from_iterable(lists))
        self._take(gone)

    def remove(self, ids) -> None:
        """Stop counting the rows with these ids (ones never counted are skipped)."""
        gone = {"all": [], "positive": [], "negative": []}
        for i in ids:
            old = self._rows.pop(i, None)
            if old is not None:
                gone["all"].append(old.words)
                if old.label in gone:
                    gone[old.label].append(old.words)
        self._take(gone)

    def _take(self, gone: dict) -> None:
        """Take what some rows counted back out: counted up first (quick), then taken off a word at a time. A
        word no row uses any more goes, so every count left is above zero (as top relies on)."""
        for key, lists in gone.items():
            if lists:
                less, counts = Counter(chain.from_iterable(lists)), self.counts[key]
                counts.subtract(less)
                for word in less:
                    if counts[word] <= 0:
                        del counts[word]

    def ids(self):
        return self._rows.keys()

    @staticmethod
    def top(*parts: WordCounts, n: int = 100) -> list[tuple[str, int, float]]:
        """The ``n`` words used by the most items across ``parts``: [(word, items, lean)], where lean runs
        from -1 (every item using it negative) to +1 (every one positive). Ties go alphabetically, so the same
        rows always make the same cloud."""
        alls = [p.counts["all"] for p in parts]
        if len(alls) == 1:
            total = alls[0]
        else:
            # Adding whole counts up took ~60 ms with a big subreddit's comments. A word outside the biggest part's
            # top n (and its ties) is used by no more items than each of those, unless another part uses it, so
            # only those words and the other parts' are added up.
            big = max(range(len(alls)), key=lambda i: len(alls[i]))
            best = heapq.nlargest(n, alls[big].items(), key=itemgetter(1))
            least = best[-1][1] if len(best) == n else 0
            words = {w for w, k in alls[big].items() if k >= least} if least > 0 else set(alls[big])
            for i, c in enumerate(alls):
                if i != big:
                    words.update(c)
            total = {w: sum(c[w] for c in alls) for w in words}
        out = []
        for w, k in heapq.nsmallest(n, ((w, k) for w, k in total.items() if k > 0), key=lambda wk: (-wk[1], wk[0])):
            lean = sum(p.counts["positive"][w] - p.counts["negative"][w] for p in parts) / k
            out.append((w, k, lean))
        return out
