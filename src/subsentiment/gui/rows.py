"""A subreddit's saved posts or comments as the window, its tables and its word cloud read them.

`Saved` is one subreddit's posts or comments in a RowIndex: its numbers as arrays, its text read for the rows a
table draws. `RowList` gives a plain list of rows (a table filled by hand) the same face. Either is a `source`: a
row is its position in it, which stays put as a running fetch adds more. `Rows` is some of a source's rows in an
order, read as a list: what the tables show and export writes.
"""
from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from subsentiment.analysis import LABELS, Analysis
from subsentiment.gui.worker import on_thread
from subsentiment.index import RowIndex
from subsentiment.store import NO_SCORE, PACKED, column, scoring_at_least

LABEL_AT = {lab: k for k, lab in enumerate(LABELS)}
# The values a source keeps as arrays, by column: missing ones sort first (NO_SCORE, -inf), as the tables had them.
ARRAYS = {"sentiment": "sentiment", "score": "score", "created_utc": "created", "label": "label"}


class Gone(dict):
    """A row deleted since it was counted: its id, and None for the rest."""

    def __missing__(self, key):
        return None


class RowSource:
    """Rows by position, with ``ids``, ``created``, ``score``, ``sentiment`` and ``label`` arrays (as RowIndex
    keeps them). `values` gives any column of every row, for sorting and filtering; a text column read once is
    kept, and kept up as rows change. The rows a table draws are kept too, the latest few thousand."""

    KEEP = 8192  # rows kept read for drawing
    READ = 128  # rows read at once around one a table asks for
    lazy = False  # whether reading rows takes long enough that a table reads them on a thread (`kept`, `keep`)

    def __init__(self):
        self._drawn: OrderedDict[int, object] = OrderedDict()  # position -> its row, read lately
        self._values: dict[str, list] = {}  # column -> every row's value, once read
        self.version = 0  # counts changes to its rows
        self._changes: deque = deque(maxlen=64)  # (version, positions changed then), the latest

    def __len__(self) -> int:
        return len(self.ids)

    def rows(self, positions) -> list:
        """The rows at ``positions``, read from wherever they're kept; the drawn ones from memory."""
        positions = [int(p) for p in positions]
        drawn = self._drawn
        missing = list(dict.fromkeys(p for p in positions if p not in drawn))
        out = dict(zip(missing, self._read(missing))) if missing else {}
        for p in positions:
            if p not in out:
                out[p] = drawn[p]
                drawn.move_to_end(p)
        self.keep(out, out.values())
        return [out[p] for p in positions]

    def row(self, p: int, around=None):
        """Row ``p``; read with ``around`` (positions near it on screen) when it isn't kept yet."""
        got = self._drawn.get(p)
        if got is None:
            return self.rows([p] if around is None else [p, *around])[0]
        return got

    def kept(self, p: int):
        """Row ``p`` if it's kept read, else None."""
        got = self._drawn.get(p)
        if got is not None:
            self._drawn.move_to_end(p)
        return got

    def keep(self, positions, rows) -> None:
        """Keep ``rows`` (read elsewhere, at ``positions``) for drawing."""
        self._drawn.update(zip(positions, rows))
        while len(self._drawn) > self.KEEP:
            self._drawn.popitem(last=False)

    def changed_since(self, version: int) -> set | None:
        """The positions changed since ``version``; None when that's too long ago to tell."""
        if not self._changes or self._changes[0][0] > version + 1:
            return set() if version == self.version else None
        return {p for v, at in self._changes if v > version for p in at}

    def values(self, key: str):
        """Every row's ``key``: an array for the numbers kept as arrays, else a list, or None when that column
        isn't read yet (`read_values`)."""
        if key in ARRAYS:
            got = getattr(self, ARRAYS[key])
            return np.where(np.isnan(got), -np.inf, got) if key == "created_utc" else got
        return self._values.get(key)

    def read_values(self, key: str, step: int = 2000):
        """Read every row's ``key``, ``step`` rows between two yields, and keep it for `values`."""
        if key in ARRAYS or key in self._values:
            return
        got, n = [], len(self)
        for a in range(0, n, step):
            got += column(self._read(range(a, min(a + step, n))), key)
            yield
        if len(self) == n:  # (rows added meanwhile: read it again next time)
            self._values[key] = got

    def changed(self, positions, rows=None) -> None:
        """Rows at ``positions`` were saved again (or added): what's kept of them is read again, or taken from
        ``rows`` (them as they are now)."""
        positions = [int(p) for p in positions]
        self.version += 1
        self._changes.append((self.version, positions))
        for p in positions:
            self._drawn.pop(p, None)
        if rows is not None:
            self.keep(positions, rows)
        if self._values and positions:
            fresh = dict(zip(positions, self._read(positions) if rows is None else rows))
            for key, got in list(self._values.items()):
                for p in sorted(fresh):
                    if p < len(got):
                        got[p] = fresh[p][key]
                    elif p == len(got):  # added at the end
                        got.append(fresh[p][key])
                    else:  # (rows before it not read: read the column again when it's next wanted)
                        del self._values[key]
                        break

    def where(self, ids) -> np.ndarray:
        raise NotImplementedError

    def search(self, typed: str) -> np.ndarray | None:
        """Which rows have the words typed, as a mask (RowIndex.search); None to look through their text instead."""
        return None

    def _read(self, positions) -> list:
        raise NotImplementedError


class RowList(RowSource):
    """A list of rows (dicts or store.Row) as a source: a table given its rows."""

    def __init__(self, rows=()):
        super().__init__()
        self._rows = list(rows)
        self._at = {r["id"]: i for i, r in enumerate(self._rows)}
        self._arrays()

    def _arrays(self) -> None:
        rows = self._rows
        self.ids = list(self._at)
        self.created = np.array([np.nan if r.get("created_utc") is None else r["created_utc"] for r in rows], np.float64)
        self.score = np.array([NO_SCORE if r.get("score") is None else r["score"] for r in rows], np.int64)
        self.sentiment = np.array([r.get("sentiment") or 0.0 for r in rows], np.float64)
        self.label = np.array([LABEL_AT.get(r.get("label"), -1) for r in rows], np.int8)

    def __len__(self) -> int:
        return len(self._rows)

    def _read(self, positions) -> list:
        return [self._rows[p] for p in positions]

    def where(self, ids) -> np.ndarray:
        return np.array([self._at.get(i, -1) for i in ids], np.int64)

    def analysis(self, min_score: int | None) -> Analysis:
        return Analysis(scoring_at_least(self._rows, min_score))

    def add(self, rows) -> None:
        """Rows saved since: a row already here is replaced, the rest go at the end."""
        changed = []
        for r in rows:
            p = self._at.get(r["id"])
            if p is None:
                p = self._at[r["id"]] = len(self._rows)
                self._rows.append(r)
            else:
                self._rows[p] = r
            changed.append(p)
        self._arrays()
        self.changed(changed, rows)


class Saved(RowSource):
    """A subreddit's saved posts or comments (``kind``), in a RowIndex (``index``). The index may be read on a
    thread (its word counts, a text column) while the window reads rows to draw and adds a fetch's."""

    TEXT_STEP = 50_000  # rows of a text column read at once, on a thread: a quarter of a second
    lazy = True  # 128 rows' text took 70 ms to read from their days' packs, 400 the first time

    def __init__(self, db_path: str | Path, kind: str, subreddit: str):
        super().__init__()
        self.kind = kind
        self.made_at = time.time()  # what was saved before this is in it
        self.index = RowIndex(db_path, kind, subreddit)

    # The index's arrays, as they are now: a fetch's rows add to them.
    ids = property(lambda self: self.index.ids)
    created = property(lambda self: self.index.created)
    score = property(lambda self: self.index.score)
    sentiment = property(lambda self: self.index.sentiment)
    label = property(lambda self: self.index.label)

    def _read(self, positions) -> list:
        """The rows at ``positions``; one deleted since the index was read (its days deleted, the window about to
        read them again) as a `Gone` row."""
        positions = [int(p) for p in positions]
        got = {r["id"]: r for r in self.index.rows(positions)}
        ids = self.ids
        return [got.get(i) or Gone(id=i) for i in (ids[p].decode() for p in positions)]

    def where(self, ids) -> np.ndarray:
        return self.index.where(ids)

    def search(self, typed: str) -> np.ndarray | None:
        return self.index.search(typed)

    def read_values(self, key: str, step: int = 2000):
        """As RowSource's; a text column the index keeps in its packs is read on a thread, a big step at a time."""
        if key in ARRAYS or key in self._values:
            return
        if key not in PACKED[self.kind] and not (key == "post_title" and self.kind == "comments"):
            yield from super().read_values(key, step)
            return
        got, n = [], len(self)
        for a in range(0, n, self.TEXT_STEP):
            got += yield from on_thread(lambda a=a: self.index.column(key, a, min(a + self.TEXT_STEP, n)))
        if len(self) == n:
            self._values[key] = got

    def add(self, rows) -> None:
        """Count in rows a running fetch saved (new, or saved again): new ones go at the end."""
        rows = list(rows)
        if rows:
            self.index.add(rows)
            self.changed(self.where([r["id"] for r in rows]), rows)

    def rescore(self, scores) -> None:
        """New scores, [(id, sentiment, label)]."""
        scores = list(scores)
        self.index.rescore(scores)
        self.changed(self.where([i for i, _, _ in scores]))

    def analysis(self, min_score: int | None) -> Analysis:
        return self.index.analysis(min_score)

    def close(self) -> None:
        self.index.close()


def chosen(source: RowSource, days: tuple[int, int] | None, min_score: int | None) -> np.ndarray:
    """The positions of the rows created in ``days`` (all of them for None) that score at least ``min_score``,
    oldest first."""
    if not len(source):
        return np.zeros(0, np.int64)
    keep = np.ones(len(source), bool)
    if days is not None:
        created = source.created
        keep &= (created >= days[0]) & (created < days[1])
    if min_score is not None:
        score = source.score
        keep &= (score != NO_SCORE) & (score >= min_score)
    at = np.flatnonzero(keep)
    created = source.created[at]
    if len(at) > 1 and not bool((np.nan_to_num(created[1:], nan=-np.inf) >= np.nan_to_num(created[:-1], nan=-np.inf)).all()):
        at = at[np.argsort(np.nan_to_num(created, nan=-np.inf), kind="stable")]
    return at


class Rows(Sequence):
    """Some of a source's rows (``at``, positions in it), in that order, read as a list of rows: a table's or
    the span's. Rows are read in blocks as they're gone through."""

    BLOCK = 1024

    def __init__(self, source: RowSource | None = None, at=None):
        self.source = source if source is not None else RowList()
        self.at = np.zeros(0, np.int64) if at is None else np.asarray(at, np.int64)

    def __len__(self) -> int:
        return len(self.at)

    def __getitem__(self, i):
        if isinstance(i, slice):
            return Rows(self.source, self.at[i])
        return self.source.rows([self.at[i]])[0]

    def __iter__(self):
        for a in range(0, len(self.at), self.BLOCK):
            yield from self.source._read(self.at[a:a + self.BLOCK])

    def __eq__(self, other) -> bool:
        return list(self) == list(other)

    def __bool__(self) -> bool:
        return len(self.at) > 0

    def __repr__(self) -> str:
        return f"Rows({len(self)})"
