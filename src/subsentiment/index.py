"""A saved subreddit's posts or comments as numpy arrays, for a window that stays quick with millions of them.

Reading every row into Python took 12 s and half a gigabyte for 650,000 comments. A RowIndex instead holds
each row's id, time, score, sentiment and label in arrays (about 40 bytes a row), read from the column block
each day's pack keeps beside its text (store.DayPack), plus each day's word counts. Sorting, filtering by span,
label or score, the overview's numbers and the word cloud's counts are array operations on it; the text of the
rows a table shows is read from their packs' frames when it's drawn (`rows`).

Build one on a thread (`RowIndex(db_path, kind, subreddit)`): numpy, zstd and SQLite let go of the interpreter
lock while they work, so the window keeps painting. After that, the window's thread changes it (`add`, `rescore`)
and draws from it (`rows`), and other threads may count words, sort its arrays or read a column meanwhile.
"""
from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import numpy as np

from subsentiment.analysis import LABEL_AT, LABELS, Analysis, words_in
from subsentiment.store import (COMMENT_COLUMNS, NO_SCORE, NOT_IMAGE, PACKED, POST_COLUMNS, SCORE_STEPS as STEPS,
                                 CommentRow, DayPack, PostRow, Search, score_bucket)

LOOSE, HELD = -1, -2  # a row's pack when its text is in its row, or in a pack saved before packs had frames
CLOSED = object()


class RowIndex:
    """One saved subreddit's posts or comments (``kind``), oldest first, as arrays: ``ids`` (bytes), ``created``,
    ``score`` (NO_SCORE: none yet), ``sentiment``, ``label`` (an index into LABELS, -1 for another). Image posts and
    their comments are left out, as everywhere. With ``words``, the words they use are counted too (`top_words`)."""

    def __init__(self, db_path: str | Path, kind: str, subreddit: str, words: bool = True):
        self.kind, self.subreddit, self.with_words = kind, subreddit.lower(), words
        self._db_path = db_path
        self.db = sqlite3.connect(db_path, check_same_thread=False)  # built on a thread, read on the window's
        self._packs: list[tuple[int, int, float]] = []  # (day, rowid in the packs table, packed_at)
        self._reader = PackReader(db_path, kind, self.subreddit, self._packs, self.db)  # the window's
        self._bulk: PackReader | None = None  # `column`'s, on a connection of its own
        self._lock = threading.Lock()  # around _changes and _bulk, touched from two threads
        self._sorted_ids: np.ndarray | None = None  # positions in id order, for `where`
        self._keys: np.ndarray | None = None  # each row's Search.key, for `search`
        self._key_order: np.ndarray | None = None  # positions in key order
        # Word counts (DayPack.word_keys): the packs' (_wkeys, _wn), pack i's in _wfrom[i]:_wfrom[i + 1], and what the
        # rows read one by one change of them, as (day, keys, n) pieces. _loose: those rows' word ids, by id.
        self._wfrom = np.zeros(1, np.int64)
        self._wkeys = self._wn = np.zeros(0, np.int64)
        self._changes: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        self._loose: dict[bytes, np.ndarray] = {}
        with self._snapshot():  # the vocab table and the packs' text read in _join too
            self._join(self._read_packs() + [self._read_rows()])
        self._merge_changes()
        self._count_all()
        self.ordered = len(self.ids)  # rows [0, ordered) are oldest first; `add` appends the rest in any order

    # Reading -------------------------------------------------------------------------------------------------

    @contextmanager
    def _snapshot(self):
        """Reads inside see the database as it was at the first one, whatever a fetch saves meanwhile."""
        self.db.execute("BEGIN")
        try:
            yield
        finally:
            self.db.commit()

    def _read_packs(self) -> list[dict]:
        """Each pack's rows, as dicts of arrays, and its word counts as (keys, n)."""
        parts = []
        for day, rowid, packed_at, cols, words in self.db.execute(
                "SELECT day, rowid, packed_at, cols, words FROM packs WHERE kind = ? AND subreddit = ? "
                "AND cols IS NOT NULL ORDER BY day", (self.kind, self.subreddit)):
            part = dict(DayPack.decode_cols(cols))
            n = len(part["ids"])
            part["pack"] = np.full(n, len(self._packs), np.int32)
            part["at"] = np.arange(n, dtype=np.int32)
            if self.with_words:
                part["counts"] = DayPack.decode_counts(words)
            self._packs.append((day, rowid, packed_at))
            parts.append(part)
        return parts

    def _read_rows(self) -> dict:
        """The rows whose numbers aren't in a pack's column block: not packed yet (or saved again since), set a
        new score since (packed 2), or in a pack saved before packs had column blocks."""
        table = self.kind
        text = PACKED[self.kind]
        visible = "image = 0" if self.kind == "posts" else NOT_IMAGE.replace("c.", "")
        sql = (f"SELECT id, created_utc, score, sentiment, label, packed, {', '.join(text)} FROM {table} "
               f"WHERE subreddit = ? COLLATE NOCASE AND packed != 1 AND {visible}")
        args: list = [self.subreddit]
        old = [d for (d,) in self.db.execute("SELECT day FROM packs WHERE kind = ? AND subreddit = ? AND cols IS NULL",
                                              (self.kind, self.subreddit))]
        if old:  # packed before packs had column blocks (a database being upgraded): read their rows one by one
            sql += (f" UNION ALL SELECT id, created_utc, score, sentiment, label, packed, {', '.join(text)} FROM {table} "
                    f"WHERE subreddit = ? COLLATE NOCASE AND packed = 1 AND {visible} AND CAST(created_utc / 86400 AS "
                    f"INTEGER) IN ({', '.join('?' * len(old))})")
            args += [self.subreddit, *old]
            sql += " ORDER BY 2"  # a day's rows together: each old pack is read whole once, not once a row
        rows = self.db.execute(sql, args).fetchall()
        part = {"ids": np.array([r[0].encode() for r in rows], dtype="S1" if not rows else None),
                "created": np.array([np.nan if r[1] is None else r[1] for r in rows], np.float64),
                "score": np.array([NO_SCORE if r[2] is None else r[2] for r in rows], np.int32),
                "sentiment": np.array([r[3] or 0.0 for r in rows], np.float64),
                "label": np.array([LABEL_AT.get(r[4], -1) for r in rows], np.int8),
                "hidden": np.zeros(len(rows), np.int8),
                "pack": np.full(len(rows), LOOSE, np.int32), "at": np.zeros(len(rows), np.int32),
                "packed": np.array([r[5] for r in rows], np.int8)}
        if self.with_words:
            texts = []
            for r in rows:
                values = list(r[6:])
                if r[5]:  # packed (2, or in a pack without a column block): its text is in its day's pack
                    values = self._reader.whole(self.kind, r[1]).get(r[0], values)
                texts.append((values[2] or "") if self.kind == "comments" else f"{values[0] or ''} {values[1] or ''}")
            part["split"] = [words_in(t) for t in texts]
        return part

    def _join(self, parts: list[dict]) -> None:
        """Put the packs' rows and the rows read one by one together, oldest first. A row in both is taken from
        its row (its numbers are newer), keeping its place in its pack when its text is still there."""
        loose = parts[-1]
        packs = parts[:-1]
        vocab = [w for (w,) in self.db.execute("SELECT word FROM vocab WHERE subreddit = ? ORDER BY id", (self.subreddit,))]
        self.vocab = vocab
        self._shared = len(vocab)  # ids below this are the vocab table's, the same in every index of the subreddit
        self._word_ids = {w: i for i, w in enumerate(vocab)} if self.with_words else {}
        cols = ("ids", "created", "score", "sentiment", "label", "hidden", "pack", "at")
        if packs:
            joined = {c: np.concatenate([p[c] for p in packs]) for c in cols}
        else:
            joined = {c: loose[c][:0] for c in cols}
        n_packed = len(joined["ids"])
        keep = joined["hidden"] == 0
        if self.with_words and packs:
            self._wkeys = np.concatenate([p["counts"][0] for p in packs])
            self._wn = np.concatenate([p["counts"][1] for p in packs])
            self._wfrom = np.concatenate([[0], np.cumsum([len(p["counts"][0]) for p in packs])])
        # Rows read one by one replace their pack's entry; one packed 2 keeps its pack's text.
        if len(loose["ids"]) and n_packed:
            order = np.argsort(joined["ids"], kind="stable")
            found = np.searchsorted(joined["ids"], loose["ids"], sorter=order)
            found = np.minimum(found, n_packed - 1)
            hit = joined["ids"][order[found]] == loose["ids"]
            where = order[found[hit]]
            keep[where] = False
            if self.with_words:  # what they counted in their pack is taken back out
                seen = joined["hidden"][where] == 0
                was = [loose["split"][r] if loose["packed"][r] == 2 else self._words_of(  # rescored: same text
                    self._reader.whole(self.kind, float(joined["created"][a])).get(joined["ids"][a].decode()))
                    for r, a in zip(np.flatnonzero(hit)[seen], where[seen])]
                at = where[seen]
                self._change(was, joined["created"][at], joined["label"][at], joined["score"][at], -1)
            stays = hit & (loose["packed"] == 2)
            loose["pack"][stays] = joined["pack"][order[found[stays]]]
            loose["at"][stays] = joined["at"][order[found[stays]]]
            held = (~hit) & (loose["packed"] != 0)
        else:
            held = loose["packed"] != 0
        loose["pack"][held] = HELD  # in a pack without a column block
        for c in cols:
            joined[c] = np.concatenate([joined[c][keep], loose[c]]) if n_packed else loose[c]
        if self.with_words:
            lists = [np.array([self._word_id(w) for w in ws], np.int64) for ws in loose["split"]]
            self._change(lists, loose["created"], loose["label"], loose["score"], 1)
            self._loose = dict(zip(loose["ids"], lists))
        created = joined["created"]
        if len(created) < 2 or bool((created[1:] >= created[:-1]).all()):
            order = slice(None)  # already oldest first, as packs are read
        else:
            order = np.argsort(created, kind="stable")
        self.ids = joined["ids"][order]
        self.created = joined["created"][order]
        self.score = joined["score"][order]
        self.sentiment = joined["sentiment"][order]
        self.label = joined["label"][order]
        self._pack_of = joined["pack"][order]
        self._at = joined["at"][order]

    def _merge_changes(self) -> list:
        """The word count changes in one piece (merged outside the lock), as a list to read."""
        with self._lock:
            pieces = list(self._changes)
        if len(pieces) < 2:
            return pieces
        merged = tuple(np.concatenate(c) for c in zip(*pieces))
        with self._lock:
            if all(a is b for a, b in zip(self._changes, pieces)):  # not merged by another thread meanwhile
                self._changes = [merged] + self._changes[len(pieces):]
            return list(self._changes)

    def _words_of(self, text) -> tuple[str, ...]:
        """The words a row with this text (values of PACKED[kind]) counts."""
        if text is None:
            return ()
        return tuple(words_in(Search.text(self.kind, text)))

    def _change(self, words, created, label, score, sign: int) -> None:
        """Count rows' words (lists of words or of word ids) in, or with ``sign`` -1 take them back out, as rows
        of these days, labels and scores."""
        lists = [w if isinstance(w, np.ndarray) else np.array([self._word_id(x) for x in w], np.int64) for w in words]
        sizes = np.array([len(w) for w in lists], np.int64)
        if not sizes.sum():
            return
        combo = DayPack.word_keys(np.zeros(len(lists), np.int64), np.asarray(label, np.int64),
                                  score_bucket(np.asarray(score, np.int64)))
        days = np.where(np.isnan(created), -1, np.asarray(created) // 86400).astype(np.int64)
        piece = (np.repeat(days, sizes), np.concatenate(lists) * 32 + np.repeat(combo, sizes),
                 np.full(int(sizes.sum()), sign, np.int64))
        with self._lock:
            self._changes.append(piece)

    def _word_id(self, word: str) -> int:
        """A word's id, numbered past the vocab table's when it isn't packed anywhere yet."""
        i = self._word_ids.get(word)
        if i is None:
            i = self._word_ids[word] = len(self.vocab)
            self.vocab.append(word)
        return i

    # Counting ------------------------------------------------------------------------------------------------

    def _count_all(self) -> None:
        days = self.created // 86400
        ok = ~np.isnan(days)
        self._d0 = int(days[ok].min()) if ok.any() else 0  # the first day counted
        size = int(days[ok].max()) - self._d0 + 1 if ok.any() else 0
        self._n = np.zeros((size, len(LABELS), len(STEPS) + 2), np.int64)
        self._sum = np.zeros(self._n.shape, np.float64)
        self._count(np.arange(len(self.ids)), 1)

    def _count(self, positions: np.ndarray, sign: int) -> None:
        """Count these rows into the overview's numbers, or with ``sign`` -1 take them back out."""
        days = self.created[positions] // 86400
        ok = ~np.isnan(days) & (self.label[positions] >= 0)
        positions, days = positions[ok], days[ok].astype(np.int64)
        if not len(positions):
            return
        low, high = int(days.min()), int(days.max())
        if low < self._d0 or high >= self._d0 + len(self._n):  # days outside the ones counted so far
            first = min(low, self._d0) if len(self._n) else low
            last = max(high, self._d0 + len(self._n) - 1) if len(self._n) else high
            grown = (last - first + 1, *self._n.shape[1:])
            n, total = np.zeros(grown, np.int64), np.zeros(grown, np.float64)
            if len(self._n):
                n[self._d0 - first:self._d0 - first + len(self._n)] = self._n
                total[self._d0 - first:self._d0 - first + len(self._n)] = self._sum
            self._n, self._sum, self._d0 = n, total, first
        shape = self._n.shape
        key = ((days - self._d0) * shape[1] + self.label[positions]) * shape[2] + score_bucket(self.score[positions])
        self._n += sign * np.bincount(key, minlength=self._n.size).reshape(shape)
        self._sum += sign * np.bincount(key, weights=self.sentiment[positions], minlength=self._n.size).reshape(shape)

    def analysis(self, min_score: int | None = None) -> Analysis:
        """The overview's numbers of the rows scoring at least ``min_score`` (store.scoring_at_least), every day."""
        if min_score is None:
            n, total = self._n.sum(axis=2), self._sum.sum(axis=2)
        elif min_score in STEPS:
            first = STEPS.index(min_score) + 2
            n, total = self._n[:, :, first:].sum(axis=2), self._sum[:, :, first:].sum(axis=2)
        else:  # not one of the steps: counted from the rows
            keep = np.flatnonzero((self.score != NO_SCORE) & (self.score >= min_score))
            out = Analysis()
            out.add({"created_utc": None if np.isnan(c) else float(c), "sentiment": float(s), "label": LABELS[k]}
                    for c, s, k in zip(self.created[keep], self.sentiment[keep], self.label[keep]) if k >= 0)
            return out
        out = Analysis()
        for d in np.flatnonzero(n.sum(axis=1)):
            labels = dict(zip(LABELS, map(int, n[d])))
            out.days[(self._d0 + int(d)) * 86400] = [float(total[d].sum()), sum(labels.values()), labels]
        out.counts = dict(zip(LABELS, map(int, n.sum(axis=0))))
        out.total = float(total.sum())
        return out

    # Choosing rows -------------------------------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.ids)

    def between(self, days: tuple[int, int] | None) -> tuple[int, int]:
        """The positions [start, stop) of the rows created in ``days`` ([first day's start, end)) among the ones in
        time order (up to ``ordered``; `mask` has the rest too), every row for None."""
        if days is None:
            return 0, len(self.ids)
        return tuple(int(i) for i in np.searchsorted(self.created[:self.ordered], days, side="left"))

    def mask(self, days: tuple[int, int] | None = None, min_score: int | None = None,
             label: str | None = None) -> np.ndarray:
        """Which rows are in ``days``, score at least ``min_score`` and have ``label``."""
        out = np.zeros(len(self.ids), bool)
        start, stop = self.between(days)
        out[start:stop] = True
        if days is not None:  # the rows added out of time order
            tail = self.created[self.ordered:]
            out[self.ordered:] = (tail >= days[0]) & (tail < days[1])
        else:
            out[self.ordered:] = True
        if min_score is not None:
            out &= (self.score != NO_SCORE) & (self.score >= min_score)
        if label is not None:
            out &= self.label == LABELS.index(label)
        return out

    def where(self, ids) -> np.ndarray:
        """The positions of rows with these ids (str or bytes), -1 for one not here."""
        if self._sorted_ids is None:
            self._sorted_ids = np.argsort(self.ids, kind="stable")
        want = [i.encode() if isinstance(i, str) else bytes(i) for i in ids]
        if not len(self.ids) or not want:
            return np.full(len(want), -1)
        fits = np.array([len(i) <= self.ids.itemsize for i in want])  # a longer one isn't here (and would be cut short)
        cut = np.array([i if f else b"" for i, f in zip(want, fits)], dtype=self.ids.dtype)
        found = np.minimum(np.searchsorted(self.ids, cut, sorter=self._sorted_ids), len(self.ids) - 1)
        at = self._sorted_ids[found]
        return np.where(fits & (self.ids[at] == cut), at, -1)

    def search(self, typed: str) -> np.ndarray | None:
        """Which rows' text has every word typed, the last one as the start of a word (Search), as a mask; None
        when there's nothing to search for, or the database has no search index yet (being upgraded): sort or
        filter by `column` then."""
        with self._lock:
            ids = self.ids
        with self._reader.lock:
            hits = self._reader.search.matches(self.kind, typed)
        if hits is None:
            return None
        with self._lock:
            keys, order = self._keys, self._key_order
        if keys is None or len(keys) != len(ids):
            keys, order = Search.keys(ids), None
            with self._lock:
                if self.ids is ids:  # not grown meanwhile
                    self._keys, self._key_order = keys, None
        if order is None or len(order) != len(keys):
            order = np.argsort(keys)
            with self._lock:
                if self._keys is keys:
                    self._key_order = order
        out = np.zeros(len(keys), bool)
        if len(keys) and len(hits):
            at = order[np.minimum(np.searchsorted(keys, hits, sorter=order), len(keys) - 1)]
            out[at[keys[at] == hits]] = True
        return out

    # Words ---------------------------------------------------------------------------------------------------

    def word_counts(self, days: tuple[int, int] | None = None,
                    min_score: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """How many of the rows in ``days`` scoring at least ``min_score`` (None or one of STEPS) use each word, and
        how many of those are positive and negative: three arrays over ``vocab``. A sum of the days' counts."""
        if min_score is not None and min_score not in STEPS:
            raise ValueError(f"words are counted for the score filter's steps {STEPS}, not {min_score}")
        if not self.with_words:  # read without them (the first start of an old database): no words to count yet
            return tuple(np.zeros(len(self.vocab), np.int64) for _ in range(3))
        changes = self._merge_changes()
        first, last = (-2 ** 62, 2 ** 62) if days is None else (days[0] // 86400, -(-days[1] // 86400))
        a, b = np.searchsorted(np.array([d for d, _, _ in self._packs], np.int64), (first, last))
        keys, n = [self._wkeys[self._wfrom[a]:self._wfrom[b]]], [self._wn[self._wfrom[a]:self._wfrom[b]]]
        for day, k, m in changes:
            inside = (day >= first) & (day < last) if days is not None else slice(None)
            keys.append(k[inside])
            n.append(m[inside])
        keys, n = np.concatenate(keys), np.concatenate(n)
        if min_score is not None:
            kept = (keys & 7) >= STEPS.index(min_score) + 2
            keys, n = keys[kept], n[kept]
        by_label = np.bincount(keys >> 3, weights=n, minlength=4 * len(self.vocab)).astype(np.int64).reshape(-1, 4)
        return by_label.sum(axis=1), by_label[:, LABELS.index("positive")], by_label[:, LABELS.index("negative")]

    def top_words(self, days: tuple[int, int] | None = None, min_score: int | None = None,
                  n: int = 100) -> list[tuple[str, int, float]]:
        """The ``n`` words used by the most rows (as `word_counts` picks them): [(word, rows, lean)], as
        analysis.WordCounts.top gives them (lean from -1, every row using it negative, to +1; ties alphabetically)."""
        return top_words([(self, min_score)], days, n)

    # Text ----------------------------------------------------------------------------------------------------

    def text(self, i: int, reader: PackReader | None = None) -> list | None:
        """Row ``i``'s packed text (values of store.PACKED[kind]), or None when its text is in its row."""
        pack = int(self._pack_of[i])
        if pack == LOOSE:
            return None
        return (reader or self._reader).text(pack, int(self._at[i]), self.ids[i].decode(), float(self.created[i]))

    def column(self, name: str, start: int = 0, stop: int | None = None) -> list[str | None]:
        """One text column (of store.PACKED[kind], or "post_title" for comments) of rows ``start`` to ``stop``, in
        order: for sorting or filtering a table by it. Each frame is decoded once (about 5 ms per thousand
        comments), so ask for a few thousand rows at a time. It reads on a connection of its own, so a thread can
        run it while the window draws `rows`."""
        stop = len(self.ids) if stop is None else min(stop, len(self.ids))
        if name != "post_title" and name not in PACKED[self.kind] or name == "post_title" and self.kind != "comments":
            raise ValueError(f"{name} isn't a text column of {self.kind}")
        with self._lock:
            if self._bulk is CLOSED:
                raise ValueError("the index is closed")
            if self._bulk is None:
                self._bulk = PackReader(self._db_path, self.kind, self.subreddit, self._packs)
            reader = self._bulk
            ids, packs = self.ids[start:stop].copy(), self._pack_of[start:stop].copy()  # as one, whatever `add` does
            at, created = self._at[start:stop].copy(), self.created[start:stop].copy()
        ids = [i.decode() for i in ids]
        if name == "post_title":
            return reader.post_titles(ids)
        out: list = [None] * len(ids)
        unread = np.flatnonzero(packs < 0).tolist()  # read from their row, or their whole pack
        for pack in np.unique(packs[packs >= 0]):
            mine = np.flatnonzero(packs == pack)
            got = reader.column(int(pack), name, at[mine], [ids[k] for k in mine])
            for k, value in zip(mine, got):
                if value is reader.MISSING:
                    unread.append(int(k))
                else:
                    out[k] = value
        # A row saved again since it was read here has its newer text in its row.
        skipped = set(unread)
        framed = [k for k in range(len(ids)) if packs[k] >= 0 and k not in skipped]
        unread += reader.unpacked([ids[k] for k in framed], framed)
        for k, value in reader.by_id(name, [ids[k] for k in unread], created[unread]):
            out[unread[k]] = value
        return out

    def rows(self, positions) -> list:
        """Rows ``positions`` whole, as store.Store.rows gives them (`store.Row`s, without words): what a table
        draws. Their numbers and post are read by id, their text from their packs' frames."""
        positions = [int(p) for p in positions]
        ids = [self.ids[p].decode() for p in positions]
        if not ids:
            return []
        reader = self._reader
        with reader.lock:
            marks = ", ".join("?" * len(ids))
            if self.kind == "posts":
                got = {r[0]: r for r in reader.db.execute(
                    f"SELECT {', '.join(POST_COLUMNS)}, packed FROM posts WHERE id IN ({marks})", ids)}
            else:
                cols = ", ".join("c." + c for c in COMMENT_COLUMNS)
                got = {r[0]: r for r in reader.db.execute(
                    f"SELECT {cols}, c.packed, p.title, p.score, p.permalink, p.packed, p.created_utc FROM comments c "
                    f"LEFT JOIN posts p ON p.id = c.post_id WHERE c.id IN ({marks})", ids)}
            columns = POST_COLUMNS if self.kind == "posts" else COMMENT_COLUMNS
            text_at = [columns.index(c) for c in PACKED[self.kind]]
            checked: set = set()  # posts' days whose packs were checked to be the ones read, in this call
            out = []
            for p, rid in zip(positions, ids):
                r = got.get(rid)
                if r is None:  # deleted since
                    continue
                values = list(r[:len(columns)])
                if r[len(columns)]:  # its text is in its pack (packed since this index was read, if it was loose then)
                    text = self.text(p, reader) if self._pack_of[p] != LOOSE else None
                    if text is None:
                        text = reader.whole(self.kind, float(self.created[p])).get(rid)
                    for k, v in zip(text_at, text or ()):
                        values[k] = v
                if self.kind == "comments":
                    title, post_score, link, post_packed, post_created = r[len(columns) + 1:]
                    if post_packed and post_created is not None:
                        title, link = reader.post(values[1], post_created, checked) or (title, link)
                    if link is not None and values[9] == link + values[0] + "/":
                        values[9] = None  # rebuilt as read, as Store.rows has it
                    values += [title, post_score, link]
                out.append(tuple.__new__(PostRow if self.kind == "posts" else CommentRow, values + [None]))
        return out

    # Changes -------------------------------------------------------------------------------------------------

    def add(self, rows) -> None:
        """Count in rows a running fetch saved (store.Row or dicts with the columns): a row already here is
        replaced (saved again, so its text is in its row now), the rest are added at the end, so every row keeps
        its position. Rows not the newest leave the end out of time order (past ``ordered``) until `reorder`."""
        rows = list(rows)
        if not rows:
            return
        at = self.where([r["id"] for r in rows])
        old = at[at >= 0]
        self._count(old, -1)
        if self.with_words and len(old):
            self._change([self._words_at(int(a)) for a in old], self.created[old], self.label[old], self.score[old], -1)
        new = [r for r, a in zip(rows, at) if a < 0]
        for r, a in zip(rows, at):
            if a >= 0:
                self.created[a] = np.nan if r["created_utc"] is None else r["created_utc"]
                self.score[a] = NO_SCORE if r["score"] is None else r["score"]
                self.sentiment[a] = r["sentiment"] or 0.0
                self.label[a] = LABEL_AT.get(r["label"], -1)
                self._pack_of[a] = LOOSE
        self._count(old, 1)
        start = len(self.ids)
        if new:
            grown = {"ids": np.concatenate([self.ids, np.array([r["id"].encode() for r in new])]),
                     "created": np.concatenate([self.created, [np.nan if r["created_utc"] is None else r["created_utc"] for r in new]]),
                     "score": np.concatenate([self.score, np.array([NO_SCORE if r["score"] is None else r["score"] for r in new], np.int32)]),
                     "sentiment": np.concatenate([self.sentiment, [r["sentiment"] or 0.0 for r in new]]),
                     "label": np.concatenate([self.label, np.array([LABEL_AT.get(r["label"], -1) for r in new], np.int8)]),
                     "_pack_of": np.concatenate([self._pack_of, np.full(len(new), LOOSE, np.int32)]),
                     "_at": np.concatenate([self._at, np.zeros(len(new), np.int32)])}
            if self._sorted_ids is not None:  # (`where` made it above) the new ids go in their places in id order:
                # sorting every id again took 65 ms for 190,000 comments, at each thread of comments a fetch saved
                added = grown["ids"][start:]
                order = np.argsort(added, kind="stable")
                places = np.searchsorted(self.ids, added[order], sorter=self._sorted_ids)
                self._sorted_ids = np.insert(self._sorted_ids, places, start + order)
            with self._lock:  # all at once, for a thread reading them together (`column`)
                for name, values in grown.items():
                    setattr(self, name, values)
                if self._keys is not None:
                    self._keys = np.concatenate([self._keys, Search.keys(grown["ids"][start:])])
                    self._key_order = None
            self._count(np.arange(start, len(self.ids)), 1)
        if self.with_words:
            changed = np.concatenate([old, np.arange(start, len(self.ids))]).astype(np.int64)
            words = [np.array([self._word_id(w) for w in self._split(r)], np.int64)
                     for r in [r for r, a in zip(rows, at) if a >= 0] + new]
            self._loose.update(zip(self.ids[changed], words))
            self._change(words, self.created[changed], self.label[changed], self.score[changed], 1)
        tail = self.created[max(start - 1, 0):]
        if new and self.ordered == start and np.all(tail[:-1] <= tail[1:]):
            self.ordered = len(self.ids)  # all the newest, in order

    def _split(self, row) -> tuple[str, ...]:
        words = getattr(row, "words", None)
        if words is not None:
            return words
        if self.kind == "comments":
            return tuple(words_in(row["body"] or ""))
        return tuple(words_in(f"{row['title'] or ''} {row['selftext'] or ''}"))

    def _words_at(self, i: int, split=None) -> np.ndarray:
        """Row ``i``'s word ids, as it's counted now: when it's packed, those of ``split`` (its words, as they were
        split from its text elsewhere), or else split again from its pack's text."""
        got = self._loose.get(self.ids[i])
        if got is None:
            words = self._words_of(self.text(i)) if split is None else split
            got = np.array([self._word_id(w) for w in words], np.int64)
        return got

    def reorder(self) -> np.ndarray:
        """Put every row back oldest first. Returns the old position of each row's new one (identity when they
        were in order already)."""
        if self.ordered == len(self.ids):
            return np.arange(len(self.ids))
        order = np.argsort(self.created, kind="stable")
        moved = {name: getattr(self, name)[order] for name in ("ids", "created", "score", "sentiment", "label", "_pack_of", "_at")}
        with self._lock:
            for name, values in moved.items():
                setattr(self, name, values)
            if self._keys is not None:
                self._keys = self._keys[order]
                self._key_order = None
        self.ordered = len(self.ids)
        self._sorted_ids = None
        return order

    def rescore(self, scores, split=None) -> np.ndarray:
        """New scores, [(id, sentiment, label)], as store.Store.rescore saves them; returns their positions (-1 for
        one not here). A row's words move to its new label: a packed one's are ``split``'s (each one's words, as
        analysis.words_in split its text elsewhere), or else split again from its text, so rescoring most of a
        subreddit that way is quicker as a new index."""
        scores = list(scores)
        at = self.where([i for i, _, _ in scores])
        known = at >= 0
        positions = at[known]
        self._count(positions, -1)
        split = [None] * len(scores) if split is None else split
        words = [self._words_at(a, w) for a, w, k in zip(at.tolist(), split, known) if k] if self.with_words else []
        if words:
            self._change(words, self.created[positions], self.label[positions], self.score[positions], -1)
        self.sentiment[positions] = [s for (_, s, _), k in zip(scores, known) if k]
        self.label[positions] = [LABEL_AT.get(lab, -1) for (_, _, lab), k in zip(scores, known) if k]
        self._count(positions, 1)
        if words:
            self._change(words, self.created[positions], self.label[positions], self.score[positions], 1)
        return at

    def close(self) -> None:
        with self._lock:
            bulk, self._bulk = self._bulk, CLOSED
        self._reader.close()
        if isinstance(bulk, PackReader):
            bulk.close()


class PackReader:
    """Reads one subreddit's packed text, on a connection and with caches of its own: the window draws rows
    through one while a thread reads a whole column through another. Each call holds its lock."""

    FRAMES = 256  # frames of text kept read, about 50 KB each
    MISSING = object()  # a value `column` couldn't read from the frames

    def __init__(self, db_path, kind: str, subreddit: str, packs: list, db: sqlite3.Connection | None = None):
        self.db = db if db is not None else sqlite3.connect(db_path, check_same_thread=False)
        self.kind, self.subreddit, self.packs = kind, subreddit, packs  # packs: the index's (day, rowid, packed_at)
        self.lock = threading.RLock()
        self._heads: dict[int, tuple[dict, int] | None] = {}  # pack -> its header and where its frames start
        self._frames: OrderedDict = OrderedDict()  # (pack, frame) -> its columns (DayPack.frame_columns)
        self._whole: OrderedDict = OrderedDict()  # (kind, day) -> (packed_at, a whole pack's text), read lately
        self._posts: OrderedDict = OrderedDict()  # day -> (packed_at, {post id: (title, permalink)}), for comments
        self.search = Search(self.db, create=False)

    def _packed_at(self, kind: str, day: int):
        row = self.db.execute("SELECT packed_at FROM packs WHERE kind = ? AND subreddit = ? AND day = ?",
                              (kind, self.subreddit, day)).fetchone()
        return row[0] if row else None

    def whole(self, kind: str, created: float, checked: set | None = None) -> dict[str, list]:
        """A whole day's pack, {id: [values of PACKED[kind]]}: for a row whose place in it isn't known. ``checked``:
        (kind, day)s already checked to be packed as read, in this call."""
        with self.lock:
            key = (kind, int(created // 86400))
            hit = self._whole.get(key)
            if hit is not None and checked is not None and key in checked:
                self._whole.move_to_end(key)
                return hit[1]
            now = self._packed_at(*key)
            if checked is not None:
                checked.add(key)
            if hit is None or hit[0] != now:  # not read yet, or packed again since
                row = self.db.execute("SELECT data FROM packs WHERE kind = ? AND subreddit = ? AND day = ?",
                                      (kind, self.subreddit, key[1])).fetchone()
                hit = self._whole[key] = (now, DayPack.read(row[0], self.db)["rows"] if row else {})
                while len(self._whole) > 8:
                    self._whole.popitem(last=False)
            else:
                self._whole.move_to_end(key)
            return hit[1]

    def block(self, pack: int, block: int) -> list | None:
        """A pack's keys (block -1) or one of its frames (block 0 on), as DayPack.frame_columns, read from disk the
        first time; None when the pack was packed again since the index was read, or is one frame of JSON."""
        with self.lock:
            key = (pack, block)
            hit = self._frames.get(key)
            if hit is not None:
                self._frames.move_to_end(key)
                return hit
            if self._heads.get(pack, ()) is None:
                return None
            day, rowid, packed_at = self.packs[pack]
            row = self.db.execute("SELECT rowid, packed_at FROM packs WHERE kind = ? AND subreddit = ? AND day = ?",
                                  (self.kind, self.subreddit, day)).fetchone()
            if row is None or row[1] != packed_at:
                return None
            if pack not in self._heads:
                head = self._read(row[0], 0, 8)
                self._heads[pack] = None  # one frame of JSON: read whole
                if not head.startswith(DayPack.MAGIC):
                    return None
                self._heads[pack] = DayPack.header(head + self._read(row[0], 8, int.from_bytes(head[4:8], "little")))
            head, start = self._heads[pack]
            keys, prose = DayPack.split_columns(head["columns"])
            bounds = [head["keys"]] + head["frames"]
            begin, end = (0, bounds[0]) if block < 0 else (bounds[block], bounds[block + 1])
            raw = DayPack.decompressor(head["dict"], self.db).decompress(self._read(row[0], start + begin, end - begin))
            hit = DayPack.frame_columns(raw, len(keys) + 1 if block < 0 else len(prose))
            self._frames[key] = hit
            while len(self._frames) > self.FRAMES:
                self._frames.popitem(last=False)
            return hit

    def _read(self, rowid: int, offset: int, size: int) -> bytes:
        if hasattr(self.db, "blobopen"):  # Python 3.11 on: only these bytes come off the disk
            with self.db.blobopen("packs", "data", rowid, readonly=True) as blob:
                blob.seek(offset)
                return blob.read(size)
        return self.db.execute("SELECT substr(data, ?, ?) FROM packs WHERE rowid = ?", (offset + 1, size, rowid)).fetchone()[0]

    def text(self, pack: int, at: int, rid: str, created: float) -> list | None:
        """The packed text of row ``rid``, at ``at`` in pack ``pack`` (HELD: place unknown)."""
        with self.lock:
            if pack >= 0:
                keys, frame = self.block(pack, -1), self.block(pack, at // DayPack.FRAME)
                if keys is not None and frame is not None and DayPack.value(keys[0], at) == rid:
                    return DayPack.assemble(self._heads[pack][0], keys, frame, at)
            return self.whole(self.kind, created).get(rid)

    def column(self, pack: int, name: str, at: np.ndarray, ids: list[str]) -> list:
        """Column ``name`` of the rows at ``at`` in pack ``pack``, each frame decoded once; MISSING for a row not
        there (its pack read whole instead, or packed again since)."""
        with self.lock:
            keys = self.block(pack, -1)
            if keys is None:
                return [self.MISSING] * len(ids)
            names, prose = DayPack.split_columns(self._heads[pack][0]["columns"])
            got = {}
            if name in names:
                got[None] = DayPack.values(keys[names.index(name) + 1])
            else:
                for frame in np.unique(at // DayPack.FRAME).tolist():
                    block = self.block(pack, frame)
                    if block is not None:
                        got[frame] = DayPack.values(block[prose.index(name)])
            out = []
            for a, rid in zip(at.tolist(), ids):
                values = got.get(None if name in names else a // DayPack.FRAME)
                ok = values is not None and DayPack.value(keys[0], a) == rid
                out.append(values[a if name in names else a % DayPack.FRAME] if ok else self.MISSING)
            return out

    def by_id(self, name: str, ids: list[str], created: np.ndarray):
        """(k, value) of column ``name`` of rows ``ids``, from their rows, or their whole packs once packed."""
        with self.lock:
            got = {}
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                got.update((i, (packed, v)) for i, packed, v in self.db.execute(
                    f"SELECT id, packed, {name} FROM {self.kind} WHERE id IN ({', '.join('?' * len(chunk))})", chunk))
            at, checked = PACKED[self.kind].index(name), set()
            for k, rid in enumerate(ids):
                packed, value = got.get(rid, (0, None))
                if packed:
                    value = (self.whole(self.kind, float(created[k]), checked).get(rid) or [None] * len(PACKED[self.kind]))[at]
                yield k, value

    def unpacked(self, ids: list[str], keys: list) -> list:
        """The ``keys`` of the rows ``ids`` whose text is in their row now (saved again since they were packed)."""
        with self.lock:
            fresh = set()
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                fresh.update(i for (i,) in self.db.execute(
                    f"SELECT id FROM {self.kind} WHERE packed = 0 AND id IN ({', '.join('?' * len(chunk))})", chunk))
            return [key for i, key in zip(ids, keys) if i in fresh]

    def post(self, post_id: str, created: float, checked: set | None = None) -> tuple[str | None, str | None] | None:
        """A packed post's title and permalink, from its day's pack, read once (only these two kept). ``checked``:
        days already checked to be packed as read, in this call."""
        with self.lock:
            day = int(created // 86400)
            hit = self._posts.get(day)
            if hit is None or checked is None or day not in checked:
                now = self._packed_at("posts", day)
                if hit is None or hit[0] != now:
                    rows = self.whole("posts", created) if now is not None else {}
                    at = PACKED["posts"].index("title"), PACKED["posts"].index("permalink")
                    hit = self._posts[day] = (now, {i: (v[at[0]], v[at[1]]) for i, v in rows.items()})
                    while len(self._posts) > 1024:  # about three years of days
                        self._posts.popitem(last=False)
                if checked is not None:
                    checked.add(day)
            self._posts.move_to_end(day)
            return hit[1].get(post_id)

    def post_titles(self, ids: list[str]) -> list[str | None]:
        """The titles of the posts of comments ``ids``."""
        with self.lock:
            titles, checked = {}, set()
            for k in range(0, len(ids), 500):
                chunk = ids[k:k + 500]
                for cid, pid, title, packed, created in self.db.execute(
                        "SELECT c.id, p.id, p.title, p.packed, p.created_utc FROM comments c LEFT JOIN posts p ON "
                        f"p.id = c.post_id WHERE c.id IN ({', '.join('?' * len(chunk))})", chunk):
                    if packed and created is not None:
                        title = (self.post(pid, created, checked) or (title, None))[0]
                    titles[cid] = title
            return [titles.get(i) for i in ids]

    def close(self) -> None:
        with self.lock:
            self.db.close()


def top_words(parts, days: tuple[int, int] | None = None, n: int = 100) -> list[tuple[str, int, float]]:
    """The ``n`` words used by the most rows of some RowIndexes of one subreddit (its posts and comments, say), each
    with its own score filter: ``parts`` is [(index, min_score)]. [(word, rows, lean)], as analysis.WordCounts.top
    gives them (lean from -1, every row using it negative, to +1; ties alphabetically)."""
    parts = list(parts)
    shared = min((ix._shared for ix, _ in parts), default=0)  # ids every index's vocab has the same
    every = positive = negative = np.zeros(shared, np.int64)
    counted = []
    for ix, min_score in parts:
        counts = ix.word_counts(days, min_score)
        every, positive, negative = (a + c[:shared] for a, c in zip((every, positive, negative), counts))
        counted.append((ix.vocab, counts))
    vocab = parts[0][0].vocab if parts else []
    # Only words that can be among the n biggest are gone through one by one: at least as many as the n-th biggest
    # count of a set of words each counted no more than it really is (the words every index has, with one index's
    # others). A new subreddit's words are all past the vocab table, and going through each of them, every time the
    # word cloud was counted during a fetch, kept the window waiting.
    used = np.flatnonzero(every)
    least = 1
    for _, counts in counted:
        known = np.concatenate([every[used], counts[0][shared:][counts[0][shared:] > 0]])
        if len(known) > n:
            least = max(least, int(np.partition(known, len(known) - n)[len(known) - n]))
    used = used[every[used] >= least]
    words = [(vocab[i], int(every[i]), int(positive[i]), int(negative[i])) for i in used]
    # The rest, by word: only ones some index counts at least least / len(parts) times can add up to least, each
    # then counted in every index.
    maybe = {names[i] for names, counts in counted
             for i in np.flatnonzero(counts[0][shared:] * len(parts) >= least) + shared}
    extra: dict[str, list[int]] = {w: [0, 0, 0] for w in maybe}
    for (ix, _), (_, counts) in zip(parts, counted):
        for w, got in extra.items():
            i = ix._word_ids.get(w, -1)
            if i >= shared and i < len(counts[0]):
                for k in range(3):
                    got[k] += int(counts[k][i])
    words += [(w, a, p, q) for w, (a, p, q) in extra.items() if a]
    return sorted(((w, a, (p - q) / a) for w, a, p, q in words), key=lambda w: (-w[1], w[0]))[:n]
