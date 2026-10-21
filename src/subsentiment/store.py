"""SQLite storage for fetched posts/comments and their sentiment scores."""
from __future__ import annotations

import csv
import hashlib
import json
import multiprocessing
import os
import re
import sqlite3
import struct
import sys
import threading
import time
from collections import OrderedDict, namedtuple
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from operator import attrgetter, itemgetter
from pathlib import Path

import numpy as np
import zstandard

from subsentiment.analysis import LABELS, words_in
from subsentiment.reddit import Comment, Post, is_image_post
from subsentiment.sentiment import Lexicon

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    title TEXT, selftext TEXT, author TEXT,
    score INTEGER, upvote_ratio REAL, num_comments INTEGER,
    created_utc REAL, permalink TEXT, url TEXT,
    sentiment REAL, label TEXT,
    fetched_at REAL,
    packed INTEGER NOT NULL DEFAULT 0  -- 1: the PACKED columns are NULL here and kept in the day's pack
    -- plus image: see Store.__init__, which adds it to databases saved before it existed
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    parent_id TEXT, author TEXT, body TEXT,
    score INTEGER, created_utc REAL, depth INTEGER, permalink TEXT,
    sentiment REAL, label TEXT,
    fetched_at REAL,
    packed INTEGER NOT NULL DEFAULT 0
);
-- The text of a subreddit's posts or comments created on one settled UTC day (day: its start / 86400),
-- compressed together: short texts barely shrink one by one, a day of them about 4x. data holds the text in
-- frames of a few hundred rows, so one row's text is read without the rest of the day's; cols the rows'
-- numbers and words how many of them use each word, for the window's RowIndex. See DayPack and Store.pack_settled.
CREATE TABLE IF NOT EXISTS packs (
    kind TEXT NOT NULL, subreddit TEXT NOT NULL, day INTEGER NOT NULL, packed_at REAL, data BLOB NOT NULL,
    PRIMARY KEY (kind, subreddit, day)  -- subreddit lowercased
    -- plus cols and words: see Store.__init__, which adds them to databases saved before they existed
);
-- The zstd dictionary a subreddit's packs of one kind are compressed with, made from its first big day.
CREATE TABLE IF NOT EXISTS dicts (
    kind TEXT NOT NULL, subreddit TEXT NOT NULL, data BLOB NOT NULL,
    PRIMARY KEY (kind, subreddit)  -- subreddit lowercased
) WITHOUT ROWID;
-- The words of a subreddit's packed posts and comments, numbered in the order they were first packed.
CREATE TABLE IF NOT EXISTS vocab (
    subreddit TEXT NOT NULL, id INTEGER NOT NULL, word TEXT NOT NULL,
    PRIMARY KEY (subreddit, id)  -- subreddit lowercased
) WITHOUT ROWID;
-- One row per post whose comments ikari has asked for: whether that worked, and whether the post's
-- scores were settled at the time (if not, its comments are fetched again once they are).
CREATE TABLE IF NOT EXISTS comment_fetches (
    post_id TEXT PRIMARY KEY,
    ok INTEGER, settled INTEGER, fetched_at REAL
);
-- Ranges whose whole content ikari has fetched: kind "posts" keys a UTC day (its start, in epoch
-- seconds) and kind "comments" keys a post id; "comments_so_far" keys a post whose whole thread was fetched
-- before the archive re-checked it, so more comments may still come. Used to show a day or thread as fully downloaded even
-- when the archive's totals (which count removed items) are higher than what could be saved.
CREATE TABLE IF NOT EXISTS complete (
    kind TEXT NOT NULL, subreddit TEXT NOT NULL, key TEXT NOT NULL, fetched_at REAL,  -- subreddit lowercased
    PRIMARY KEY (kind, subreddit, key)
);
-- Posts the archive has that were removed on Reddit, which ikari skips. Its daily counts include them, so
-- Download coverage counts the ones seen as checked.
CREATE TABLE IF NOT EXISTS removed_posts (
    id TEXT PRIMARY KEY, subreddit TEXT NOT NULL, created_utc REAL  -- subreddit lowercased
);
-- The user's notes on the timeline, one per subreddit and UTC day (its start, in epoch seconds): a label of
-- up to two characters drawn on the chart, and a description shown on hover.
CREATE TABLE IF NOT EXISTS notes (
    subreddit TEXT NOT NULL, day INTEGER NOT NULL, label TEXT NOT NULL, text TEXT, saved_at REAL,  -- subreddit lowercased
    PRIMARY KEY (subreddit, day)
) WITHOUT ROWID;
-- What the user set a word or phrase to mean in one subreddit, from -1 (negative) through 0 (no sentiment) to +1
-- (positive); scoring there reads it that way (sentiment.Lexicon). word: lowercased, single-spaced.
CREATE TABLE IF NOT EXISTS words (
    subreddit TEXT NOT NULL, word TEXT NOT NULL, weight REAL NOT NULL, saved_at REAL,  -- subreddit lowercased
    PRIMARY KEY (subreddit, word)
) WITHOUT ROWID;
-- Arctic Shift's count of a subreddit's posts on each UTC day (its start, in epoch seconds), and when it was asked:
-- Download coverage asks for a day's count at most once a UTC day (CoveragePage).
CREATE TABLE IF NOT EXISTS archive_counts (
    subreddit TEXT NOT NULL, day INTEGER NOT NULL, posts INTEGER NOT NULL, counted_at REAL NOT NULL,  -- subreddit lowercased
    PRIMARY KEY (subreddit, day)
) WITHOUT ROWID;
-- (Lookups by subreddit alone use the *_fetched indexes below, which start with it.)
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id);
-- A running fetch's new rows are read back by when they were saved.
CREATE INDEX IF NOT EXISTS posts_fetched ON posts(subreddit COLLATE NOCASE, fetched_at);
CREATE INDEX IF NOT EXISTS comments_fetched ON comments(subreddit COLLATE NOCASE, fetched_at);
"""

POST_COLUMNS = ("id", "subreddit", "title", "selftext", "author", "score", "upvote_ratio",
                "num_comments", "created_utc", "permalink", "url", "sentiment", "label", "fetched_at")
COMMENT_COLUMNS = ("id", "post_id", "subreddit", "parent_id", "author", "body", "score",
                   "created_utc", "depth", "permalink", "sentiment", "label", "fetched_at")
# A comment's permalink is nearly always its post's with the comment's id added, and was the biggest thing
# saved per comment, so it's stored as NULL then and rebuilt when read.
_COMMENT_SELECT = "SELECT " + ", ".join(
    "COALESCE(c.permalink, p.permalink || c.id || '/') AS permalink" if col == "permalink" else "c." + col
    for col in COMMENT_COLUMNS) + (", c.packed, p.title AS post_title, p.score AS post_score, p.packed AS post_packed, "
                                   "p.subreddit AS post_sub, p.created_utc AS post_created "
                                   "FROM comments c LEFT JOIN posts p ON p.id = c.post_id")
SCHEMA_VERSION = 4  # 4: text searchable (Search); 1: derivable comment permalinks not stored, posts_sub and comments_sub indexes dropped
#                    2: settled days' text packed (packs table, packed columns)
#                    3: packs in frames, with each row's numbers and words (DayPack)

# The columns kept in day packs: the text, which is most of what's saved. The rest stays in its row, so
# counts, filters and lookups by id still run in SQL. (A comment's permalink is mostly NULL already, rebuilt
# from its post's.)
PACKED = {"posts": ("title", "selftext", "author", "permalink", "url"), "comments": ("parent_id", "author", "body")}
SETTLE = 2 * 86400  # a day is packed once it's this old: scores have settled, so its rows rarely change
_ZSTD_LEVEL = 12
# A row's packed column: 0 its text is in the row; 1 in its day's pack; 2 in its day's pack, but its score was set
# again since (Store.rescore), so the pack's copy of its numbers is out of date until the day is packed again.
NO_SCORE = -2 ** 31  # a score Arctic Shift hasn't filled in yet, among a pack's numbers
# The score filter's steps (the window's MIN_SCORES). A pack's word counts and RowIndex's overview numbers are kept
# per label and the bucket between two steps, so any step's are a sum. Bucket 0 is no score yet, 1 below the first.
SCORE_STEPS = (1, 10, 50, 100, 500, 1000)


def score_bucket(score: np.ndarray) -> np.ndarray:
    """Each score's (NO_SCORE: none yet) bucket between two SCORE_STEPS."""
    return np.where(score == NO_SCORE, 0, np.searchsorted(np.array(SCORE_STEPS), score, side="right") + 1).astype(np.int64)


# How many of post p's comments are saved, and whether its whole thread is (1 or 0): as the comment downloader
# leaves it, so Download coverage offers no more for it. That's a post with no comments, one whose "Download all"
# read its thread to the end (kind "comments" in the complete table), one read to the end before the archive
# counted its comments ("comments_so_far", until it has), and one with as many saved as the archive counts.
SAVED_COMMENTS = "(SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id)"
WHOLE_THREAD = (  # IFNULL: an uncounted post (num_comments NULL) would make "= 0" and ">=" NULL
    "IFNULL(p.num_comments = 0 "
    "OR EXISTS (SELECT 1 FROM complete k WHERE k.kind = 'comments' AND k.subreddit = lower(p.subreddit) AND k.key = p.id) "
    "OR (p.num_comments IS NULL AND EXISTS (SELECT 1 FROM complete k WHERE k.kind = 'comments_so_far' "
    "AND k.subreddit = lower(p.subreddit) AND k.key = p.id)) "
    f"OR (p.num_comments > 0 AND {SAVED_COMMENTS} >= p.num_comments), 0)")

# Comments on image posts are hidden along with the posts (``c`` is the comments table).
NOT_IMAGE = "c.post_id NOT IN (SELECT id FROM posts WHERE image = 1)"


def scoring_at_least(rows, min_score: int | None) -> list:
    """The posts or comments scoring at least ``min_score`` themselves; every row when None. With a filter
    on, a score Arctic Shift hasn't filled in yet (None, for about 36 hours) doesn't pass: those rows come
    back once it has. Always a new list."""
    rows = list(rows)
    if min_score is None:
        return rows
    return [r for r in rows if r["score"] is not None and r["score"] >= min_score]


class Row(tuple):
    """A saved post or comment as the window holds it: a tuple, read by column name (``r["body"]``) or, much
    quicker, as an attribute (``r.body``). With hundreds of thousands of comments saved, a tuple each takes about
    a third of the memory a dict each did. ``keys()`` and ``dict(r)`` give the columns the store reads."""
    __slots__ = ()
    COLUMNS: tuple[str, ...] = ()
    _at: dict[str, int] = {}

    def __getitem__(self, key):
        if key.__class__ is str:
            i = self._at.get(key)
            if i is not None:
                return tuple.__getitem__(self, i)
            if key in self.COLUMNS:  # worked out when read (a comment's permalink)
                return getattr(self, key)
            raise KeyError(key)
        return tuple.__getitem__(self, key)

    # The rest of a mapping's ways, so a row stands in for the dicts the window held before.
    def __iter__(self):
        return iter(self.COLUMNS)

    def __len__(self) -> int:
        return len(self.COLUMNS)

    def __contains__(self, key) -> bool:
        return key in self.COLUMNS

    def keys(self) -> tuple[str, ...]:
        return self.COLUMNS

    def items(self):
        return ((c, self[c]) for c in self.COLUMNS)

    def values(self):
        return (self[c] for c in self.COLUMNS)

    def _replace(self, **changes) -> Row:
        """This row with some of its fields changed (a new score, say)."""
        return tuple.__new__(type(self), [changes.pop(f, v) for f, v in zip(self._fields, tuple.__iter__(self))])

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default


def column(rows: list, key: str):
    """Every row's ``key``, quickly: a saved row (`Row`) is read as an attribute and an sqlite3.Row by position,
    both far quicker than by name."""
    kinds = set(map(type, rows))
    if len(kinds) == 1 and issubclass(next(iter(kinds)), Row):  # rows a fetch adds can be dicts
        return map(attrgetter(key), rows)
    if kinds == {sqlite3.Row}:
        return map(itemgetter(rows[0].keys().index(key)), rows)
    return map(itemgetter(key), rows)


class PostRow(Row, namedtuple("_Post", POST_COLUMNS + ("words",))):
    """``words``: the words its title and text use (analysis.words_in), when read for the word cloud; else None."""
    __slots__ = ()
    COLUMNS = POST_COLUMNS
    _at = {c: i for i, c in enumerate(POST_COLUMNS)}


class CommentRow(Row, namedtuple("_Comment", COMMENT_COLUMNS[:9] + ("link",) + COMMENT_COLUMNS[10:]
                                 + ("post_title", "post_score", "post_link", "words"))):
    """``link`` is the permalink as saved, mostly None: rebuilt then from its post's (``post_link``), as read.
    ``words``: as a post's, of its body."""
    __slots__ = ()
    COLUMNS = COMMENT_COLUMNS + ("post_title", "post_score")
    _at = {c: i for i, c in enumerate(COMMENT_COLUMNS[:9] + ("link",) + COMMENT_COLUMNS[10:]
                                      + ("post_title", "post_score")) if c != "link"}

    @property
    def permalink(self) -> str | None:
        if self.link is not None or self.post_link is None:
            return self.link
        return self.post_link + self.id + "/"


class DayPack:
    """How a settled day's posts or comments of a subreddit are kept, in a row of the packs table.

    ``data`` is the text (the PACKED columns): b"IKP3", the header's length, the header as JSON ({"columns",
    "rows", "keys": where the keys end, "frames": where each frame ends, "dict": the zstd dictionary's id or 0}),
    the keys (the ids, and the columns `split_columns` keeps for the whole day) as one zstd frame, then the rest of
    the text a few hundred rows a zstd frame. Each frame has its row count, then each column as a null flag a row,
    the offsets of the values and their UTF-8. A row's text is two frames' read, not the whole day's.
    Packs saved before this format are one zstd frame of JSON {"columns", "rows": {id: [values]}}; `read` takes both.

    ``cols`` is the rows' numbers in the same order, zstd-compressed: their count, the ids' width, the ids, then
    created_utc, score (NO_SCORE when there's none yet), sentiment, label (its LABELS index, -1 for another) and
    hidden (an image post, or a comment on one) as arrays. ``words`` is each row's words (`words_in`), as ids in
    the vocab table: a count a row, then the ids.
    """

    MAGIC = b"IKP3"
    FRAME = 256  # rows a frame: a frame takes about a tenth of a millisecond to read, and with a dictionary
    #              packs barely bigger than a whole day in one frame
    DICT_SIZE = 112_640
    NUMBERS = (("created", np.float64), ("score", np.int32), ("sentiment", np.float64), ("label", np.int8),
               ("hidden", np.int8))
    dicts: dict[int, zstandard.ZstdCompressionDict] = {}  # every dictionary read or made here, by its id

    @classmethod
    def frame_bytes(cls, columns: list[list]) -> bytes:
        """Columns of values (str or None) as a frame's bytes, before compressing."""
        out = [struct.pack("<I", len(columns[0]))]
        for values in columns:
            encoded = []
            for v in values:
                if v is not None and not isinstance(v, str):
                    raise TypeError(f"only text is packed, not {type(v).__name__}")
                encoded.append(b"" if v is None else v.encode())
            out.append(bytes(v is None for v in values))
            out.append(np.cumsum([0] + [len(e) for e in encoded], dtype=np.uint32).tobytes())
            out.append(b"".join(encoded))
        return b"".join(out)

    @staticmethod
    def frame_columns(raw: bytes, width: int) -> list[tuple[bytes, list[int], int, bytes]]:
        """A frame's ``width`` columns as (null flags, offsets, where the text starts, the frame), to read a row
        from without decoding the others."""
        n = struct.unpack_from("<I", raw)[0]
        at, out = 4, []
        for _ in range(width):
            nulls = raw[at:at + n]
            offsets = np.frombuffer(raw, np.uint32, n + 1, at + n).tolist()
            start = at + n + 4 * (n + 1)
            out.append((nulls, offsets, start, raw))
            at = start + offsets[-1]
        return out

    @staticmethod
    def value(column: tuple, k: int) -> str | None:
        nulls, offsets, start, raw = column
        return None if nulls[k] else raw[start + offsets[k]:start + offsets[k + 1]].decode()

    @staticmethod
    def values(column: tuple) -> list[str | None]:
        """Every value of a column (frame_columns), in order."""
        nulls, offsets, start, raw = column
        return [None if nulls[k] else raw[start + a:start + b].decode()
                for k, (a, b) in enumerate(zip(offsets, offsets[1:]))]

    @classmethod
    def encode(cls, columns, ids: list[str], text: dict, zdict: zstandard.ZstdCompressionDict | None) -> bytes:
        """The data of a pack of ``ids`` in that order, their text being ``text[id]`` (values of ``columns``). A day of
        no more than a frame of rows is kept as one frame of JSON (the format before frames): frames would only
        add their headers to it."""
        if len(ids) <= cls.FRAME:
            return zstandard.ZstdCompressor(level=_ZSTD_LEVEL).compress(
                json.dumps({"columns": list(columns), "rows": {i: text[i] for i in ids}}, ensure_ascii=False).encode())
        packer = zstandard.ZstdCompressor(level=_ZSTD_LEVEL, dict_data=zdict) if zdict else zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
        keys, prose = cls.split_columns(columns)
        at = {c: k for k, c in enumerate(columns)}
        blocks = [packer.compress(cls.frame_bytes([ids] + [[text[i][at[c]] for i in ids] for c in keys]))]
        ends = [len(blocks[0])]
        for k in range(0, len(ids), cls.FRAME):
            chunk = ids[k:k + cls.FRAME]
            blocks.append(packer.compress(cls.frame_bytes([[text[i][at[c]] for i in chunk] for c in prose])))
            ends.append(ends[-1] + len(blocks[-1]))
        header = json.dumps({"columns": list(columns), "rows": len(ids), "keys": ends[0], "frames": ends[1:],
                             "dict": zdict.dict_id() if zdict else 0}).encode()
        return cls.MAGIC + struct.pack("<I", len(header)) + header + b"".join(blocks)

    @staticmethod
    def split_columns(columns) -> tuple[list[str], list[str]]:
        """The columns kept for the whole day at once (short, alike values: links, parent ids), and the ones kept
        a frame at a time (the text itself)."""
        keys = [c for c in columns if c in ("permalink", "url", "parent_id")]
        return keys, [c for c in columns if c not in keys]

    @classmethod
    def header(cls, head: bytes) -> tuple[dict, int]:
        """A pack's header and where the rest starts, from its first bytes (at least 8, then the header's)."""
        size = struct.unpack_from("<I", head, 4)[0]
        return json.loads(head[8:8 + size]), 8 + size

    @classmethod
    def decompressor(cls, dict_id: int, db: sqlite3.Connection | None = None, schema: str = "main"):
        if not dict_id:
            return zstandard.ZstdDecompressor()
        if dict_id not in cls.dicts and db is not None:
            cls.remember(db, schema)
        return zstandard.ZstdDecompressor(dict_data=cls.dicts[dict_id])  # KeyError: its dictionary is gone

    @classmethod
    def remember(cls, db: sqlite3.Connection, schema: str = "main") -> None:
        """Keep every dictionary in a database (or an attached one) for reading its packs."""
        for (data,) in db.execute(f"SELECT data FROM {schema}.dicts"):
            d = zstandard.ZstdCompressionDict(data)
            cls.dicts[d.dict_id()] = d

    @classmethod
    def assemble(cls, head: dict, keys: list, frame: list, at: int) -> list:
        """Row ``at`` (its place in the day) of a pack: values of its columns, from the day's keys and its frame."""
        names, prose = cls.split_columns(head["columns"])
        got = dict(zip(names, (cls.value(c, at) for c in keys[1:])))
        got.update(zip(prose, (cls.value(c, at % cls.FRAME) for c in frame)))
        return [got[c] for c in head["columns"]]

    @classmethod
    def read(cls, blob: bytes, db: sqlite3.Connection | None = None, schema: str = "main") -> dict:
        """A whole pack's text, as {"columns": [...], "rows": {id: [values]}}, in either format."""
        if not blob.startswith(cls.MAGIC):
            return json.loads(zstandard.ZstdDecompressor().decompress(blob))
        head, start = cls.header(blob)
        unpacker = cls.decompressor(head["dict"], db, schema)
        names, prose = cls.split_columns(head["columns"])
        keys = cls.frame_columns(unpacker.decompress(blob[start:start + head["keys"]]), len(names) + 1)
        values = {c: cls.values(col) for c, col in zip(["id"] + names, keys)}
        for c in prose:
            values[c] = []
        begin = start + head["keys"]
        for end in head["frames"]:
            frame = cls.frame_columns(unpacker.decompress(blob[begin:start + end]), len(prose))
            begin = start + end
            for c, col in zip(prose, frame):
                values[c] += cls.values(col)
        return {"columns": head["columns"], "rows": dict(zip(values["id"], map(list, zip(*(values[c] for c in head["columns"])))))}

    @classmethod
    def encode_cols(cls, ids: list[str], numbers: dict[str, list]) -> bytes:
        encoded = [i.encode() for i in ids]
        width = max(map(len, encoded), default=1)
        parts = [struct.pack("<II", len(ids), width), np.array(encoded, dtype=f"S{width}").tobytes()]
        parts += [np.array(numbers[name], dtype=t).tobytes() for name, t in cls.NUMBERS]
        return zstandard.ZstdCompressor(level=3).compress(b"".join(parts))

    @classmethod
    def decode_cols(cls, blob: bytes) -> dict[str, np.ndarray]:
        """A pack's numbers as arrays, "ids" among them (bytes, as numpy keeps them)."""
        raw = zstandard.ZstdDecompressor().decompress(blob)
        n, width = struct.unpack_from("<II", raw)
        out = {"ids": np.frombuffer(raw, f"S{width}", n, 8)}
        at = 8 + n * width
        for name, t in cls.NUMBERS:
            out[name] = np.frombuffer(raw, t, n, at)
            at += out[name].nbytes
        return out

    # A day's word counts: how many of its rows of each label and score bucket use each word, keyed
    # word id * 32 + label (0 to 2, 3 for none) * 8 + bucket (score_bucket), sorted.
    @staticmethod
    def word_keys(words: np.ndarray, label: np.ndarray, bucket: np.ndarray) -> np.ndarray:
        """The keys of words (vocab ids) used by rows of these labels (an index into LABELS, -1 for none) and buckets."""
        return words.astype(np.int64) * 32 + np.where(label < 0, 3, label).astype(np.int64) * 8 + bucket

    @staticmethod
    def encode_counts(keys: np.ndarray, n: np.ndarray) -> bytes:
        words = (keys >> 5).astype(np.uint32)
        steps = np.diff(words, prepend=np.uint32(0))  # small numbers, which compress far better
        return zstandard.ZstdCompressor(level=9).compress(struct.pack("<I", len(keys)) + steps.tobytes()
                                                          + (keys & 31).astype(np.uint8).tobytes() + n.astype(np.uint32).tobytes())

    @staticmethod
    def decode_counts(blob: bytes) -> tuple[np.ndarray, np.ndarray]:
        """A day's word count keys and counts."""
        raw = zstandard.ZstdDecompressor().decompress(blob)
        size = struct.unpack_from("<I", raw)[0]
        words = np.cumsum(np.frombuffer(raw, np.uint32, size, 4), dtype=np.int64)
        combos = np.frombuffer(raw, np.uint8, size, 4 + 4 * size)
        return words * 32 + combos, np.frombuffer(raw, np.uint32, size, 4 + 5 * size).astype(np.int64)

    @staticmethod
    def merge_counts(keys: np.ndarray, n: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Counts with each key once, summed, and those summing to nothing left out."""
        if not len(keys):
            return keys.astype(np.int64), n.astype(np.int64)
        unique, inverse = np.unique(keys, return_inverse=True)
        total = np.bincount(inverse, weights=n, minlength=len(unique)).astype(np.int64)
        kept = total != 0
        return unique[kept], total[kept]

    @classmethod
    def train(cls, columns: list[list]) -> zstandard.ZstdCompressionDict | None:
        """A dictionary for a subreddit's packs of a kind, from a day of its rows (the prose columns, as
        `split_columns` has them), or None when the day is too small to make one worth having."""
        n = len(columns[0])
        if n < 4 * cls.FRAME:
            return None
        samples = [cls.frame_bytes([c[k:k + 64] for c in columns]) for k in range(0, n, 64)]
        try:
            return zstandard.train_dictionary(min(cls.DICT_SIZE, sum(map(len, samples)) // 20), samples)
        except zstandard.ZstdError:
            return None


class Search:
    """Full-text search of saved posts' and comments' text: a contentless FTS5 index of each kind, which keeps
    only which rows use each word (about 4 MB per 100,000 comments), keyed by `key` of each row's id. It finds
    whole words and, as the last one is typed, words starting with it; not text in the middle of a word.
    Contentless deletes need SQLite 3.43 or later: without them there's no index and `matches` is None."""

    WORD = re.compile(r"\w+")

    def __init__(self, db: sqlite3.Connection, create: bool = True):
        """``create``: make the index's tables if they aren't there (a Store does; a reader only looks)."""
        self.db = db
        try:
            for kind in PACKED:
                if create:
                    db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS search_{kind} USING fts5(text, content='', "
                               "contentless_delete=1, detail=none)")
                else:
                    db.execute(f"SELECT 1 FROM search_{kind} LIMIT 0")
            self.ready = True
        except sqlite3.OperationalError:  # an SQLite before 3.43, or one built without FTS5; or none made yet
            self.ready = False

    _DIGITS = np.full(256, -1, np.int64)
    _DIGITS[np.frombuffer(b"0123456789", np.uint8)] = np.arange(10)
    _DIGITS[np.frombuffer(b"abcdefghijklmnopqrstuvwxyz", np.uint8)] = np.arange(10, 36)
    _DIGITS[np.frombuffer(b"ABCDEFGHIJKLMNOPQRSTUVWXYZ", np.uint8)] = np.arange(10, 36)

    @classmethod
    def keys(cls, ids: np.ndarray) -> np.ndarray:
        """`key` of each id of a numpy bytes array, the base-36 ones worked out together."""
        out = np.zeros(len(ids), np.int64)
        if not len(ids):
            return out
        chars = np.frombuffer(np.ascontiguousarray(ids).tobytes(), np.uint8).reshape(len(ids), ids.itemsize)
        odd = np.zeros(len(ids), bool)
        for col in chars.T:  # numpy pads short ids with zero bytes, at the end
            digit = cls._DIGITS[col]
            odd |= (col != 0) & (digit < 0)
            out = np.where(col != 0, out * 36 + np.maximum(digit, 0), out)
        odd |= (chars != 0).sum(axis=1) > 11
        for k in np.flatnonzero(odd):
            out[k] = cls.key(ids[k].decode())
        return out

    @staticmethod
    def key(row_id: str) -> int:
        """A row's key in the index: its base-36 id as a number (below 2^57), or for any other id a hash of it
        (2^62 and up). Rowids can't be used, as VACUUM may number them again."""
        if len(row_id) <= 11 and row_id.isascii() and row_id.isalnum():
            return int(row_id, 36)
        return 2 ** 62 | int.from_bytes(hashlib.blake2b(row_id.encode(), digest_size=7).digest(), "little")

    @staticmethod
    def text(kind: str, values) -> str:
        """What's searched of a row (values of PACKED[kind])."""
        return (values[2] or "") if kind == "comments" else f"{values[0] or ''}\n{values[1] or ''}"

    def put(self, kind: str, rows) -> None:
        """Index rows [(id, values of PACKED[kind])], in place of what they had."""
        if self.ready:
            self.db.executemany(f"INSERT OR REPLACE INTO search_{kind} (rowid, text) VALUES (?, ?)",
                                [(self.key(i), self.text(kind, v)) for i, v in rows])

    def drop(self, kind: str, ids) -> None:
        if self.ready:
            self.db.executemany(f"DELETE FROM search_{kind} WHERE rowid = ?", [(self.key(i),) for i in ids])

    def optimize(self) -> None:
        """Merge the index's pieces into one per kind: about 2% smaller, and quicker to search."""
        if self.ready:
            for kind in PACKED:
                self.db.execute(f"INSERT INTO search_{kind} (search_{kind}) VALUES ('optimize')")
            self.db.commit()

    def clear(self) -> None:
        if self.ready:
            for kind in PACKED:
                self.db.execute(f"DELETE FROM search_{kind}")

    @classmethod
    def query(cls, typed: str) -> str | None:
        """An FTS5 query for every word typed, the last one as a prefix; None if nothing is."""
        words = cls.WORD.findall(typed.lower())
        return " ".join(f'"{w}"' for w in words) + "*" if words else None

    def matches(self, kind: str, typed: str) -> np.ndarray | None:
        """The keys of the rows whose text has every word typed (the last as the start of one), sorted; None when
        there's no index yet (a database being upgraded) or nothing was typed."""
        query = self.query(typed)
        if not self.ready or query is None or self.db.execute("PRAGMA user_version").fetchone()[0] < 4:
            return None
        cur = self.db.execute(f"SELECT rowid FROM search_{kind} WHERE search_{kind} MATCH ?", (query,))
        return np.sort(np.fromiter((r[0] for r in cur), np.int64))


def _unpack(blob: bytes, db: sqlite3.Connection | None = None) -> dict:
    return DayPack.read(blob, db)


def _split(texts: list[str]) -> list[tuple[str, ...]]:
    """Each text's words (`words_in`); run in worker processes by a big Store.upgrade."""
    return [tuple(words_in(t)) for t in texts]


def _pack_all(path: str, pack: bool = True) -> None:
    """A big Store.upgrade's packing (if ``pack``) and search index, in a process of its own, splitting words in a
    few more."""
    store = Store(path)
    pool = None
    try:
        if pack:
            pool = ProcessPoolExecutor(max(1, min(8, (os.cpu_count() or 2) - 1)), mp_context=multiprocessing.get_context("spawn"))
    except Exception:  # noqa: BLE001 - split them here
        pool = None
    try:
        if pack:
            store.pack_settled(vacuum=False, split=pool, old=True)
            store.drop_rebuilt_links()
            if store._left_to_pack():
                return
        store.index_text()
    finally:
        if pool is not None:
            pool.shutdown(cancel_futures=True)
        store.close()


def _storable(value):
    """``value``, with any text SQLite can't store made safe. The archive's JSON can hold a lone half of an
    emoji ("\\ud83d", from a title cut short), which SQLite refuses, failing the whole fetch."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return value.encode("utf-8", "replace").decode("utf-8")
    return value


# A spreadsheet runs a cell starting with one of these as a formula ("=HYPERLINK(...)" in a post title).
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value):
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # GUI reads while the fetch thread writes
        fresh = not self.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'posts'").fetchone()
        self.db.executescript(SCHEMA)
        for table in PACKED:
            if "packed" not in {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN packed INTEGER NOT NULL DEFAULT 0")
                self.db.commit()
            if fresh:  # an older database gets it from Store.upgrade, on its thread
                self._index_unpacked(table)
        for column in {"cols", "words"} - {r["name"] for r in self.db.execute("PRAGMA table_info(packs)")}:
            self.db.execute(f"ALTER TABLE packs ADD COLUMN {column} BLOB")
        self.db.commit()
        DayPack.remember(self.db)
        self.search = Search(self.db)
        self.db.commit()
        self._vocab: dict[str, dict[str, int]] = {}  # subreddit lowercased -> its vocab table, while packing
        # Image posts are kept as rows (so Download coverage counts them as checked and a refetch doesn't
        # download them again) but hidden from every read here and never given comments.
        if "image" not in {r["name"] for r in self.db.execute("PRAGMA table_info(posts)")}:
            self.db.execute("ALTER TABLE posts ADD COLUMN image INTEGER NOT NULL DEFAULT 0")
            self.db.executemany("UPDATE posts SET image = 1 WHERE id = ?", [
                (r["id"],) for r in self.db.execute("SELECT id, url, selftext FROM posts")
                if is_image_post(r["url"], r["selftext"])])
            self.db.commit()
        self._stamp = 0.0
        self._lexicons: dict[str, tuple] = {}  # subreddit lowercased -> (when read, its words, their Lexicon)
        backup = Path(str(self.path) + "-before-v1")
        if fresh:
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        elif (self.db.execute("PRAGMA user_version").fetchone()[0] < 1
              and self.path.resolve() not in Store._migration_tried):
            Store._migration_tried.add(self.path.resolve())  # a failed one waits for the next start, not each fetch
            self._migrate(backup)
        try:
            backup.unlink(missing_ok=True)  # left by a migration that was cut short; the database was untouched
        except OSError:  # another connection is mid-migration and has it open
            pass

    _migration_tried: set[Path] = set()
    # Recently read packs, shared by every connection (the window's loads each open their own), as
    # (path, kind, subreddit, day) -> (packed_at, unpacked); a pack replaced since has a new packed_at.
    _packs: OrderedDict = OrderedDict()
    _packs_lock = threading.Lock()

    def _migrate(self, backup: Path) -> None:
        """Shrink a database saved by an older ikari, keeping a copy of it until that's done. It's one
        transaction, checked row by row against what was there before it's committed, and skipped (to be
        tried at the next start) if anything fails, since the older layout reads and saves just as well."""
        try:
            copy = sqlite3.connect(backup)
            try:
                self.db.backup(copy)
            finally:
                copy.close()
            self.db.execute("BEGIN IMMEDIATE")
            try:
                if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
                    self.db.rollback()  # another connection did it meanwhile
                    return
                # What the comments read as now, taken inside the lock so no other save can slip between.
                self.db.execute("CREATE TEMP TABLE was AS SELECT id, permalink FROM comments")
                self.db.execute(
                    "UPDATE comments SET permalink = NULL WHERE permalink = "
                    "(SELECT p.permalink FROM posts p WHERE p.id = comments.post_id) || id || '/'")
                self.db.execute("DROP INDEX IF EXISTS posts_sub")
                self.db.execute("DROP INDEX IF EXISTS comments_sub")
                self.db.execute("PRAGMA user_version = 1")
                before = self.db.execute("SELECT COUNT(*) FROM temp.was").fetchone()[0]
                after = self.db.execute("SELECT COUNT(*) FROM main.comments").fetchone()[0]
                changed = self.db.execute(
                    f"SELECT COUNT(*) FROM temp.was o JOIN ({_COMMENT_SELECT}) n ON n.id = o.id "
                    "WHERE n.permalink IS NOT o.permalink").fetchone()[0]
                if before != after or changed:
                    raise sqlite3.DatabaseError(f"{changed} of {before} comments would read back differently")
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise
            finally:
                self.db.execute("DROP TABLE IF EXISTS temp.was")
        except (sqlite3.Error, OSError):
            return
        self._compact()

    @classmethod
    def upgrade(cls, path: str | Path, stop: threading.Event | None = None) -> None:
        """Pack every settled day of a database saved before packs existed (version 1), or before they were kept
        in frames with their rows' numbers and words (version 2), on its own connection. It can take a minute on
        a big one, so the app runs it on its own thread, and splits the words in worker processes. A copy of the
        database is kept until every settled day is packed, across starts if it's cut short: each day is its own
        transaction, checked before it's committed (see pack_settled), so a day not reached is left as it was
        and packed next time. Then (and for version 3, only) it indexes the text for search (index_text)."""
        self = cls(path)
        try:
            for table in PACKED:
                self._index_unpacked(table)
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, 2, 3):
                return
            pack = version < 3  # 3 only needs its search index
            backup = Path(str(self.path) + f"-before-v{version + 1}")
            if pack and not backup.exists():  # one from a start that was cut short holds it before any packing
                part = Path(str(backup) + "-part")
                copy = sqlite3.connect(part)
                try:
                    self.db.backup(copy)
                finally:
                    copy.close()
                os.replace(part, backup)
            rows = self.db.execute("SELECT (SELECT COUNT(*) FROM posts) + (SELECT COUNT(*) FROM comments)").fetchone()[0]
            elsewhere = rows >= 50_000 and self._pack_elsewhere(stop, pack)
            if not elsewhere and pack:
                self.pack_settled(vacuum=False, stop=stop, old=True)
                self.drop_rebuilt_links()
            if self._left_to_pack():
                return  # stopped, or some day couldn't be packed: carried on at the next start
            if not elsewhere and not self.index_text(stop) or elsewhere and (stop is not None and stop.is_set()):
                return  # cut short: indexed again from the start next time
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()
            self._compact()
            if pack:
                backup.unlink()
        except Exception:  # noqa: BLE001 - the database reads and saves as it is; the next start tries again
            pass
        finally:
            self.close()

    def _index_unpacked(self, table: str) -> None:
        """The rows a RowIndex reads one by one: those whose numbers aren't in their day's pack."""
        self.db.execute(f"CREATE INDEX IF NOT EXISTS {table}_unpacked ON {table}(subreddit COLLATE NOCASE, "
                        "created_utc) WHERE packed != 1")
        self.db.commit()

    def drop_rebuilt_links(self) -> None:
        """Clear the permalinks comments were saved with that are their post's plus their id, as they're rebuilt
        when read. Comments saved after their post was packed kept theirs (0.4 compared with the post's row, whose
        link is in its pack then): 68 of a 230 MB database."""
        posts = self.db.execute("SELECT id, subreddit, created_utc, permalink, packed FROM posts WHERE id IN "
                                "(SELECT post_id FROM comments WHERE permalink IS NOT NULL)").fetchall()
        self.db.execute("CREATE TEMP TABLE IF NOT EXISTS links (id TEXT PRIMARY KEY, permalink TEXT)")
        try:
            self.db.execute("DELETE FROM temp.links")
            self.db.executemany("INSERT INTO temp.links VALUES (?, ?)",
                                [(r["id"], r["permalink"]) for r in self._unpacked("posts", posts) if r["permalink"]])
            self.db.execute("UPDATE comments SET permalink = NULL WHERE permalink IS NOT NULL AND permalink = "
                            "(SELECT l.permalink FROM temp.links l WHERE l.id = comments.post_id) || id || '/'")
            self.db.commit()
        finally:
            self.db.execute("DROP TABLE IF EXISTS temp.links")

    def index_text(self, stop: threading.Event | None = None) -> bool:
        """Index every saved row's text for search (Search), a day of a subreddit at a time, each its own
        transaction, so a fetch saving meanwhile waits no longer than one. False if ``stop`` cut it short (it
        starts again next time: Store.upgrade marks it done)."""
        if not self.search.ready:
            return True
        self.search.clear()
        self.db.commit()
        for kind, cols in PACKED.items():
            day_rows: list = []
            last = None
            cur = self.db.execute(f"SELECT id, lower(subreddit), CAST(created_utc / 86400 AS INTEGER), packed, "
                                  f"{', '.join(cols)} FROM {kind} ORDER BY lower(subreddit), created_utc")
            for r in cur.fetchall():
                if (r[1], r[2]) != last:
                    if stop is not None and stop.is_set():
                        return False
                    self._index_day(kind, last, day_rows)
                    last, day_rows = (r[1], r[2]), []
                day_rows.append(r)
            self._index_day(kind, last, day_rows)
        self.search.optimize()
        return True

    def _index_day(self, kind: str, day: tuple | None, rows: list) -> None:
        if not rows:
            return
        packed = self._pack(kind, day[0], day[1], keep=False) if any(r[3] for r in rows) else {}
        self.search.put(kind, [(r[0], packed.get(r[0], r[4:]) if r[3] else r[4:]) for r in rows])
        self.db.commit()

    def _pack_elsewhere(self, stop: threading.Event | None, pack: bool = True) -> bool:
        """Pack in a process of its own (`_pack_all`), so a minute of it doesn't take the interpreter from the
        window this thread is in; ``stop`` ends it, between days or mid-day (its transaction is then rolled
        back). False if no process could be started here."""
        try:
            worker = multiprocessing.get_context("spawn").Process(target=_pack_all, args=(str(self.path), pack), daemon=False)
            worker.start()
        except Exception:  # noqa: BLE001
            return False
        while worker.is_alive():
            worker.join(0.2)
            if stop is not None and stop.is_set():
                worker.terminate()
                worker.join()
                return True
        return worker.exitcode == 0  # one that failed (a build missing a module, say): packed here instead

    def _left_to_pack(self) -> int:
        """Settled rows not packed yet, and packs saved before packs had their rows' numbers."""
        left = " + ".join(f"(SELECT COUNT(*) FROM {t} WHERE packed = 0 AND created_utc < ?)" for t in PACKED)
        return self.db.execute(f"SELECT {left} + (SELECT COUNT(*) FROM packs WHERE cols IS NULL)",
                               (self._settled_before(),) * len(PACKED)).fetchone()[0]

    @staticmethod
    def _settled_before(now: float | None = None) -> float:
        """The start of the first day too recent to pack."""
        return ((now if now is not None else time.time()) - SETTLE) // 86400 * 86400

    def pack_settled(self, now: float | None = None, vacuum: bool = True, stop: threading.Event | None = None,
                     split: ProcessPoolExecutor | None = None, old: bool = False) -> int:
        """Pack every settled day that has rows not packed yet (ones saved since the day was last packed, or
        never packed), rows whose scores were set since (packed 2), or a pack saved in the format before
        frames when ``old`` (Store.upgrade's, which runs it in a process of its own). Each day is one transaction:
        the new pack is checked to read back exactly the text it replaces before the rows' copies are cleared.
        ``split``: worker processes to split the words in. Returns how many days were packed."""
        self.db.commit()
        before = self._settled_before(now)
        days: dict[tuple, set[str]] = {}  # (kind, subreddit, day) -> ids of its rows not packed yet
        for kind in PACKED:
            for i, sub, day in self.db.execute(
                    f"SELECT id, lower(subreddit), CAST(created_utc / 86400 AS INTEGER) FROM {kind} "
                    "WHERE packed != 1 AND created_utc < ?", (before,)):
                days.setdefault((kind, sub, day), set()).add(i)
            for sub, day in self.db.execute("SELECT subreddit, day FROM packs WHERE kind = ? AND cols IS NULL", (kind,)) if old else ():
                days.setdefault((kind, sub, day), set())
        done = 0
        for (kind, sub, day), ids in sorted(days.items()):
            if stop is not None and stop.is_set():
                break
            try:
                self.db.execute("BEGIN IMMEDIATE")
                vocab = len(self._vocab.get(sub, ()))
                try:
                    self._write_pack(kind, sub, day, ids, split=split)
                    self.db.commit()
                    done += 1
                except BaseException:
                    self.db.rollback()
                    if sub in self._vocab:  # words it numbered weren't saved
                        self._vocab[sub] = dict(list(self._vocab[sub].items())[:vocab])
                    raise
            except Exception:  # noqa: BLE001 - busy, or text that can't be packed: leave the day as it is
                continue
        if done and vacuum:  # the cleared text leaves its pages part empty, which only a VACUUM gives back
            self._compact()
        return done

    def _write_pack(self, kind: str, sub: str, day: int, fresh, drop=(), split: ProcessPoolExecutor | None = None) -> None:
        """Pack one day again, inside a transaction: the rows already in its pack (less ``drop``) and the rows
        ``fresh`` names whose text or numbers changed since. Rows no longer saved are left out; a day left
        with none has its pack removed."""
        cols = PACKED[kind]
        # Read inside the lock, so a row saved again since is packed with its new text; one another connection
        # packed meanwhile is skipped, its text being in the old pack read here.
        packed = self._pack(kind, sub, day, cached=False)  # in the old pack's order
        text = {i: v for i, v in packed.items() if i not in drop}
        old = self.db.execute("SELECT cols, words FROM packs WHERE kind = ? AND subreddit = ? AND day = ?",
                              (kind, sub, day)).fetchone()
        new_text = set()
        fresh = [i for i in fresh if i not in drop]
        for k in range(0, len(fresh), 500):
            chunk = fresh[k:k + 500]
            for r in self.db.execute(f"SELECT id, {', '.join(cols)} FROM {kind} WHERE packed = 0 AND "
                                     f"id IN ({', '.join('?' * len(chunk))})", chunk):
                text[r["id"]] = [r[c] for c in cols]
                new_text.add(r["id"])
        hidden = "image" if kind == "posts" else "post_id IN (SELECT id FROM posts WHERE image = 1)"
        numbers = {}
        ids = list(text)
        for k in range(0, len(ids), 500):
            chunk = ids[k:k + 500]
            for r in self.db.execute(f"SELECT id, created_utc, score, sentiment, label, {hidden} FROM {kind} "
                                     f"WHERE id IN ({', '.join('?' * len(chunk))})", chunk):
                numbers[r[0]] = r
        ids = sorted(numbers, key=lambda i: (numbers[i][1], i))
        if not ids:
            self.db.execute("DELETE FROM packs WHERE kind = ? AND subreddit = ? AND day = ?", (kind, sub, day))
            return
        prose = [cols.index(c) for c in DayPack.split_columns(cols)[1]]
        zdict = self._dictionary(kind, sub, [[text[i][c] for i in ids] for c in prose])
        blob = DayPack.encode(cols, ids, text, zdict)
        if DayPack.read(blob, self.db) != {"columns": list(cols), "rows": {i: text[i] for i in ids}}:
            raise sqlite3.DatabaseError(f"{kind} of r/{sub} on day {day} wouldn't read back the same")
        label = {lab: k for k, lab in enumerate(LABELS)}
        packed_numbers = {"created": [numbers[i][1] for i in ids],
                          "score": [NO_SCORE if numbers[i][2] is None else numbers[i][2] for i in ids],
                          "sentiment": [numbers[i][3] if numbers[i][3] is not None else 0.0 for i in ids],
                          "label": [label.get(numbers[i][4], -1) for i in ids],
                          "hidden": [int(bool(numbers[i][5])) for i in ids]}
        counts = self._word_counts(kind, sub, ids, text, packed_numbers, packed, new_text, old, split)
        self.db.execute("INSERT OR REPLACE INTO packs (kind, subreddit, day, packed_at, data, cols, words) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (kind, sub, day, time.time(), blob,
                                                         DayPack.encode_cols(ids, packed_numbers), DayPack.encode_counts(*counts)))
        # A row saved again since that isn't in ``fresh`` keeps its newer text in its row (packed 0).
        self.db.executemany(f"UPDATE {kind} SET packed = 1, {', '.join(c + ' = NULL' for c in cols)} "
                            "WHERE id = ? AND packed = 0", [(i,) for i in new_text])
        self.db.executemany(f"UPDATE {kind} SET packed = 1 WHERE id = ? AND packed = 2", [(i,) for i in ids])

    def _word_counts(self, kind, sub, ids, text, numbers, packed, new_text, old, split) -> tuple[np.ndarray, np.ndarray]:
        """A day's word counts (DayPack.word_keys) for its new pack: its old pack's, less what the rows that changed
        since counted there and plus what they count now, so only those rows' text is split again. Every row's
        is when the old pack has no counts."""
        new = {i: k for k, i in enumerate(ids)}
        now = DayPack.word_keys(np.zeros(len(ids), np.int64), np.array(numbers["label"], np.int64),
                                score_bucket(np.array(numbers["score"], np.int64)))  # each row's label and bucket
        visible = np.array(numbers["hidden"]) == 0
        before: dict[str, int] = {}  # id -> its label and bucket in the old pack, for one counted there
        if old is not None and old["cols"] is not None and old["words"] is not None:
            was = DayPack.decode_cols(old["cols"])
            keys = DayPack.word_keys(np.zeros(len(was["ids"]), np.int64), was["label"].astype(np.int64),
                                     score_bucket(was["score"].astype(np.int64)))
            before = {i.decode(): int(k) for i, k, h in zip(was["ids"], keys, was["hidden"]) if not h}
            old_keys, old_n = DayPack.decode_counts(old["words"])
        else:
            old_keys = old_n = np.zeros(0, np.int64)
        less = [i for i, k in before.items()  # gone, saved again, hidden or rescored since
                if i not in new or i in new_text or not visible[new[i]] or now[new[i]] != k]
        more = [i for i in ids if visible[new[i]] and (i not in before or i in less)]
        texts = [(packed if i in less else text)[i] for i in less] + [text[i] for i in more]
        split_words = self._split_all([(t[2] or "") if kind == "comments" else f"{t[0] or ''} {t[1] or ''}"
                                       for t in texts], split)
        vocab = self._vocabulary(sub)
        known = len(vocab)
        combos = [before[i] for i in less] + [int(now[new[i]]) for i in more]
        signs = [-1] * len(less) + [1] * len(more)
        keys, n = [old_keys], [old_n]
        for words, combo, sign in zip(split_words, combos, signs):
            if words:
                keys.append(np.array([vocab.setdefault(w, len(vocab)) for w in words], np.int64) * 32 + combo)
                n.append(np.full(len(words), sign, np.int64))
        if len(vocab) > known:
            self.db.executemany("INSERT INTO vocab VALUES (?, ?, ?)", [(sub, k, w) for w, k in list(vocab.items())[known:]])
        keys, n = DayPack.merge_counts(np.concatenate(keys), np.concatenate(n))
        if (n < 0).any():
            raise sqlite3.DatabaseError(f"{kind} of r/{sub}: the old pack's word counts don't match its rows")
        return keys, n

    @staticmethod
    def _split_all(texts: list[str], pool: ProcessPoolExecutor | None) -> list[tuple[str, ...]]:
        if pool is None or len(texts) < 2000:
            return _split(texts)
        try:
            return [w for part in pool.map(_split, [texts[k:k + 2000] for k in range(0, len(texts), 2000)]) for w in part]
        except Exception:  # noqa: BLE001 - the processes died: split them here
            return _split(texts)

    def _vocabulary(self, sub: str) -> dict[str, int]:
        """A subreddit's vocab table as {word: id}, kept while packing."""
        vocab = self._vocab.get(sub)
        if vocab is None or len(vocab) != self.db.execute(
                "SELECT COALESCE(MAX(id) + 1, 0) FROM vocab WHERE subreddit = ?", (sub,)).fetchone()[0]:  # another connection added some
            vocab = self._vocab[sub] = {w: i for i, w in self.db.execute(
                "SELECT id, word FROM vocab WHERE subreddit = ? ORDER BY id", (sub,))}
        return vocab

    def _dictionary(self, kind: str, sub: str, columns: list[list]) -> zstandard.ZstdCompressionDict | None:
        """The dictionary a subreddit's packs of a kind are compressed with: made from this day's rows if it
        has none yet and they're enough."""
        row = self.db.execute("SELECT data FROM dicts WHERE kind = ? AND subreddit = ?", (kind, sub)).fetchone()
        if row is not None:
            d = zstandard.ZstdCompressionDict(row[0])
        else:
            d = DayPack.train(columns)
            if d is None:
                return None
            self.db.execute("INSERT INTO dicts VALUES (?, ?, ?)", (kind, sub, d.as_bytes()))
        DayPack.dicts[d.dict_id()] = d
        return d

    def _pack(self, kind: str, sub: str, day: int, cached: bool = True, keep: bool = True) -> dict[str, list]:
        """One day's packed text, as {id: [values of PACKED[kind]]}; {} if there's no pack. Unless ``keep``, a
        pack read is not kept for the next read (a whole subreddit's, which would keep its text twice)."""
        key = (kind, sub, day)
        row = self.db.execute("SELECT packed_at FROM packs WHERE kind = ? AND subreddit = ? AND day = ?", key).fetchone()
        if row is None:
            return {}
        with Store._packs_lock:
            hit = Store._packs.get((self.path, *key))
            if cached and hit and hit[0] == row[0]:
                Store._packs.move_to_end((self.path, *key))
                return hit[1]
        data = _unpack(self.db.execute("SELECT data FROM packs WHERE kind = ? AND subreddit = ? AND day = ?", key).fetchone()[0], self.db)
        rows = data["rows"] if data["columns"] == list(PACKED[kind]) else {  # packed with other columns
            i: [dict(zip(data["columns"], v)).get(c) for c in PACKED[kind]] for i, v in data["rows"].items()}
        if not keep:
            return rows
        with Store._packs_lock:
            Store._packs[self.path, *key] = (row[0], rows)
            while len(Store._packs) > 512:  # about a year of days of a subreddit or two
                Store._packs.popitem(last=False)
        return rows

    def _unpacked(self, kind: str, rows) -> list[dict]:
        """``rows`` as dicts, with the text of packed ones filled in from their day's pack, and for comments
        their post's title when that post is packed."""
        out = [dict(r) for r in rows]
        packs: dict[tuple, dict] = {}  # each day's pack looked up once in this read
        shown = [i for i, c in enumerate(PACKED[kind]) if out and c in out[0]]  # the packed columns read

        def pack(k: str, sub: str, created: float) -> dict:
            key = (k, sub.lower(), int(created // 86400))
            if key not in packs:
                packs[key] = self._pack(*key)
            return packs[key]
        for r in out:
            if r.get("packed"):
                text = pack(kind, r["subreddit"], r["created_utc"]).get(r["id"])  # pack_settled checks no
                if text:  # packed row is missing from its pack
                    for i in shown:
                        r[PACKED[kind][i]] = text[i]
            if r.get("post_packed"):
                post = dict(zip(PACKED["posts"], pack("posts", r["post_sub"], r["post_created"]).get(r["post_id"], ())))
                r["post_title"] = post.get("title")
                if r["permalink"] is None and post.get("permalink") is not None:
                    r["permalink"] = post["permalink"] + r["id"] + "/"
            for k in ("post_packed", "post_sub", "post_created"):
                r.pop(k, None)
        return out

    def day_totals(self, kind: str, subreddit: str, min_score: int | None = None) -> list[tuple]:
        """How many of a subreddit's posts or comments (``kind``) scoring at least ``min_score`` each day has with
        each label, and their sentiment added up: [(day start, label, items, sum)]. Counted here, where reading
        the rows of a big subreddit to count them takes seconds (analysis.Analysis.from_days)."""
        if kind == "posts":
            sql = "SELECT created_utc, label, sentiment, score FROM posts WHERE subreddit = ? COLLATE NOCASE AND image = 0"
        else:
            sql = ("SELECT c.created_utc, c.label, c.sentiment, c.score FROM comments c "
                   "WHERE c.subreddit = ? COLLATE NOCASE AND " + NOT_IMAGE)
        where = "created_utc IS NOT NULL" + ("" if min_score is None else " AND score IS NOT NULL AND score >= ?")
        return self.db.execute(
            f"SELECT CAST(created_utc / 86400 AS INTEGER) * 86400, label, COUNT(*), SUM(sentiment) FROM ({sql}) "
            f"WHERE {where} GROUP BY 1, 2", (subreddit, *([] if min_score is None else [min_score]))).fetchall()

    def rows(self, kind: str, subreddit: str, since: float | None = None, words: bool = False,
             days: tuple[float, float] | None = None) -> list[Row]:
        """Every saved post or comment (``kind``) of a subreddit as `Row`s, oldest first; or with ``since``, those
        saved at or after it, in the order they were saved (what a running fetch has added). Strings many rows
        share (a comment's post id and title, its author) are kept once. With ``words``, each row's text is split
        into the words the word cloud counts (``Row.words``), once, here: the window reads a big subreddit on a
        thread, and splitting 650,000 comments took longer than reading them. With ``days`` ([start, end)), only the
        rows posted then."""
        if kind == "posts":
            cols = ", ".join(POST_COLUMNS)
            sql = (f"SELECT {cols}, packed, NULL, NULL, NULL, NULL, NULL, NULL FROM posts "
                   "WHERE subreddit = ? COLLATE NOCASE AND image = 0")
        else:
            cols = ", ".join("c." + c for c in COMMENT_COLUMNS)
            sql = (f"SELECT {cols}, c.packed, p.id, p.title, p.score, p.permalink, p.packed, "
                   "CAST(p.created_utc / 86400 AS INTEGER) FROM comments c LEFT JOIN posts p ON p.id = c.post_id "
                   "WHERE c.subreddit = ? COLLATE NOCASE AND " + NOT_IMAGE)
        if days is not None:
            sql += " AND created_utc >= ? AND created_utc < ?"
        sql += " AND fetched_at >= ? ORDER BY fetched_at" if since is not None else " ORDER BY created_utc"
        if kind == "comments":
            sql = sql.replace(" fetched_at", " c.fetched_at").replace(" created_utc", " c.created_utc")
        cur = self.db.cursor()
        cur.row_factory = None
        columns = POST_COLUMNS if kind == "posts" else COMMENT_COLUMNS
        n, text_at = len(columns), [columns.index(c) for c in PACKED[kind]]
        created_at, author_at, sub_at = columns.index("created_utc"), columns.index("author"), columns.index("subreddit")
        lower, bulk = subreddit.lower(), since is None
        shared: dict = {}  # each string many rows share, once
        packs: dict[str, dict] = {"posts": {}, "comments": {}}  # by kind, the days' packs read so far
        posts: dict[str, tuple] = {}  # a comment's post: (its id, title, permalink), once each

        def pack(k: str, day: int) -> dict:
            got = packs[k].get(day)
            if got is None:
                if bulk and k == kind and len(packs[k]) > 4:  # rows come a day at a time: done with the others
                    packs[k].clear()
                got = packs[k][day] = self._pack(k, lower, day, keep=not bulk)
            return got

        out = []
        make, cls, share, intern = tuple.__new__, PostRow if kind == "posts" else CommentRow, shared.setdefault, sys.intern
        # Read as it goes rather than all at once, so what each row is read into is let go of meanwhile.
        for r in cur.execute(sql, (subreddit, *(days or ()), *([since] if since is not None else []))):
            values = list(r[:n])
            if r[n]:  # its text is in its day's pack
                text = pack(kind, int(r[created_at] // 86400)).get(r[0])
                if text:
                    for i, v in zip(text_at, text):
                        values[i] = v
            values[author_at], values[sub_at], values[-2] = (  # -2: the label
                share(values[author_at], values[author_at]), share(values[sub_at], values[sub_at]),
                share(values[-2], values[-2]))
            if kind == "comments":
                post = posts.get(values[1])
                if post is None:
                    title, link = r[n + 2], r[n + 4]
                    if r[n + 5]:  # the post's text is in its own day's pack: title, selftext, author, permalink
                        title, _, _, link = (pack("posts", r[n + 6]).get(r[n + 1]) or (title, None, None, link))[:4]
                    post = posts[values[1]] = (values[1], title, link)
                values[1] = post[0]
                if post[2] is not None and values[9] == post[2] + values[0] + "/":  # rebuilt as read instead
                    values[9] = None
                values += (post[1], r[n + 3], post[2])
            if words:
                text = values[5] if kind == "comments" else f"{values[2] or ''} {values[3] or ''}"
                values.append(tuple(map(intern, words_in(text or ""))))
            else:
                values.append(None)
            out.append(make(cls, values))
        return out

    def _saved_at(self) -> float:
        """Now, as ``fetched_at``, later than every row this store saved before. Windows' clock moves in
        15.6 ms steps, so a thread's comments saved together all had one time, and every read of what a
        fetch added since the last row (``posts_since``) got the whole batch back again."""
        self._stamp = max(time.time(), self._stamp + 1e-6)
        return self._stamp

    def close(self) -> None:
        # Copy what's committed into the main file now, so a process ended right after (see gui.app.end_now)
        # leaves it there and not only in the WAL file.
        try:
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:  # busy with a fetch's writes: the WAL file still holds them safely
            pass
        self.db.close()

    def save_post(self, post: Post, sentiment: float, label: str) -> None:
        values = (post.id, post.subreddit, post.title, post.selftext, post.author, post.score,
                  post.upvote_ratio, post.num_comments, post.created_utc, post.permalink, post.url,
                  sentiment, label, self._saved_at(), int(post.image))
        values = tuple(map(_storable, values))
        # Comments whose permalink is rebuilt from this post's get their own copy if it's about to change.
        old = self.db.execute("SELECT id, subreddit, created_utc, permalink, packed FROM posts WHERE id = ?",
                              (values[0],)).fetchall()
        link = self._unpacked("posts", old)[0]["permalink"] if old else None
        if link is not None and link != values[9]:
            self.db.execute("UPDATE comments SET permalink = ? || id || '/' WHERE post_id = ? AND permalink IS NULL",
                            (link, values[0]))
        self.db.execute(f"INSERT OR REPLACE INTO posts ({','.join(POST_COLUMNS)}, image) "
                        f"VALUES ({','.join('?' * len(values))})", values)
        self.search.put("posts", [(values[0], (values[2], values[3]))])

    def save_comment(self, c: Comment, sentiment: float, label: str) -> None:
        post = self.db.execute("SELECT id, subreddit, created_utc, permalink, packed FROM posts WHERE id = ?",
                               (c.post_id,)).fetchall()
        link = self._unpacked("posts", post)[0]["permalink"] if post else None  # in its pack once it's packed
        permalink = None if link is not None and c.permalink == f"{link}{c.id}/" else c.permalink
        values = (c.id, c.post_id, c.subreddit, c.parent_id, c.author, c.body, c.score,
                  c.created_utc, c.depth, permalink, sentiment, label, self._saved_at())
        values = tuple(map(_storable, values))
        self.db.execute(f"INSERT OR REPLACE INTO comments ({', '.join(COMMENT_COLUMNS)}) "
                        f"VALUES ({', '.join('?' * len(values))})", values)
        self.search.put("comments", [(values[0], (None, None, values[5]))])

    def comment_ids(self, post_id: str) -> set[str]:
        """The ids of the comments saved on one post."""
        return {r[0] for r in self.db.execute("SELECT id FROM comments WHERE post_id = ?", (post_id,))}

    def posts_between(self, subreddit: str, after: float | None = None,
                      before: float | None = None) -> list[sqlite3.Row]:
        """Stored posts created in [after, before), each with has_comments and, when ikari has asked for
        its comments before, comments_ok and comments_settled. Image posts are included (image = 1): the
        caller needs them to skip, not to show."""
        return self._unpacked("posts", self.db.execute(
            "SELECT p.*, EXISTS(SELECT 1 FROM comments c WHERE c.post_id = p.id) AS has_comments, "
            "f.ok AS comments_ok, f.settled AS comments_settled, p.image "
            "FROM posts p LEFT JOIN comment_fetches f ON f.post_id = p.id WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ?",
            (subreddit, after if after is not None else float("-inf"),
             before if before is not None else float("inf"))))

    def mark_comments(self, post_id: str, ok: bool, settled: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO comment_fetches VALUES (?, ?, ?, ?)",
                        (post_id, int(ok), int(settled), time.time()))

    def mark_complete(self, kind: str, subreddit: str, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO complete VALUES (?, ?, ?, ?)", (kind, subreddit.lower(), key, time.time()))

    def complete_keys(self, kind: str, subreddit: str) -> set[str]:
        return {r["key"] for r in self.db.execute(
            "SELECT key FROM complete WHERE kind = ? AND subreddit = ?", (kind, subreddit.lower()))}

    def read_spans(self, subreddit: str) -> list[tuple[int, int]]:
        """Stretches of time (first, last second) fetches have read every post of (ArcticShiftClient.reached),
        so a stopped download carries on from where it got to."""
        return sorted(tuple(map(int, k.split(":"))) for k in self.complete_keys("posts_read", subreddit))

    def save_read_spans(self, subreddit: str, spans) -> None:
        merged: list[tuple[int, int]] = []
        for a, b in sorted(self.read_spans(subreddit) + list(spans)):
            if merged and a <= merged[-1][1] + 1:
                merged[-1] = (merged[-1][0], max(merged[-1][1], b))
            else:
                merged.append((a, b))
        self.db.execute("DELETE FROM complete WHERE kind = 'posts_read' AND subreddit = ?", (subreddit.lower(),))
        for a, b in merged:
            self.mark_complete("posts_read", subreddit, f"{a}:{b}")

    def save_removed(self, subreddit: str, removed: dict[str, float]) -> None:
        self.db.executemany("INSERT OR REPLACE INTO removed_posts VALUES (?, ?, ?)",
                            [(pid, subreddit.lower(), created) for pid, created in removed.items()])

    def removed_per_day(self, subreddit: str, after: float, before: float) -> dict[int, int]:
        """Removed posts seen per UTC day in [after, before), as {day start: count}; ones also saved
        (removed after ikari got them) are counted as saved instead."""
        rows = self.db.execute(
            "SELECT CAST(created_utc / 86400 AS INTEGER) * 86400 d, COUNT(*) n FROM removed_posts "
            "WHERE subreddit = ? AND created_utc >= ? AND created_utc < ? "
            "AND id NOT IN (SELECT id FROM posts) GROUP BY d",
            (subreddit.lower(), after, before))
        return {r["d"]: r["n"] for r in rows}

    def save_archive_counts(self, subreddit: str, counts: dict[int, int], at: float | None = None) -> None:
        at = time.time() if at is None else at
        self.db.executemany("INSERT OR REPLACE INTO archive_counts VALUES (?, ?, ?, ?)",
                            [(subreddit.lower(), int(d), int(n), at) for d, n in counts.items()])
        self.db.commit()

    def archive_counts(self, subreddit: str, after: float, before: float, since: float = 0) -> dict[int, int]:
        """The archive's post count of each UTC day in [after, before) that was asked for at or after ``since``."""
        rows = self.db.execute("SELECT day, posts FROM archive_counts WHERE subreddit = ? AND day >= ? AND day < ? "
                               "AND counted_at >= ?", (subreddit.lower(), after, before, since))
        return {r["day"]: r["posts"] for r in rows}

    def open_threads(self, subreddit: str, days, every: bool = False, cap: int = 0) -> list[str]:
        """Saved posts from these UTC days (their starts) whose comment threads aren't saved whole, nor, with a
        ``cap``, have that many comments saved; with ``every`` all of them. Newest first: what Download
        coverage's comment downloads fetch."""
        days = sorted(days)
        if not days:
            return []
        rows = self.db.execute(
            "SELECT p.id, CAST(p.created_utc / 86400 AS INTEGER) * 86400 d FROM posts p WHERE p.subreddit = ? "
            "COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? AND p.image = 0 "
            + ("" if every else f"AND NOT {WHOLE_THREAD} ") + ("" if every or not cap else f"AND {SAVED_COMMENTS} < ? ")
            + "ORDER BY p.created_utc DESC",
            (subreddit, days[0], days[-1] + 86400) + (() if every or not cap else (cap,)))
        want = set(days)
        return [r["id"] for r in rows if r["d"] in want]

    def posts_per_day(self, subreddit: str, after: float, before: float, images: bool = False) -> dict[int, int]:
        """Saved posts per UTC day in [after, before), as {day start: count}; ``images`` counts the image
        posts that were skipped instead (the archive's daily totals include them)."""
        rows = self.db.execute(
            "SELECT CAST(created_utc / 86400 AS INTEGER) * 86400 d, COUNT(*) n FROM posts "
            "WHERE subreddit = ? COLLATE NOCASE AND created_utc >= ? AND created_utc < ? AND image = ? GROUP BY d",
            (subreddit, after, before, int(images)))
        return {r["d"]: r["n"] for r in rows}

    def comment_coverage(self, subreddit: str, after: float, before: float) -> list[sqlite3.Row]:
        """Saved posts created in [after, before), newest first, each with saved_comments (how many of
        its comments are saved) beside the post's own num_comments, and whole (see WHOLE_THREAD)."""
        return self._unpacked("posts", self.db.execute(
            "SELECT p.id, p.subreddit, p.title, p.num_comments, p.score, p.created_utc, p.permalink, p.packed, "
            f"{SAVED_COMMENTS} AS saved_comments, {WHOLE_THREAD} AS whole "
            "FROM posts p WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? "
            "AND p.image = 0 ORDER BY p.created_utc DESC", (subreddit, after, before)))

    def comment_counts(self, subreddit: str, after: float | None, before: float | None) -> dict[str, int]:
        """How many comments are saved on each post created in [after, before) that has any."""
        return dict(self.db.execute(
            "SELECT c.post_id, COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id "
            "WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? GROUP BY c.post_id",
            (subreddit, after if after is not None else float("-inf"),
             before if before is not None else float("inf"))).fetchall())

    def whole_threads_per_day(self, subreddit: str, after: float, before: float) -> dict[int, tuple[int, int]]:
        """Per UTC day in [after, before), {day start: (posts whose comment threads are saved whole, saved
        posts)}, image posts left out as in posts_per_day. Counted in SQL: no post is read."""
        rows = self.db.execute(
            f"SELECT CAST(p.created_utc / 86400 AS INTEGER) * 86400 d, COUNT(*) n, SUM({WHOLE_THREAD}) whole "
            "FROM posts p WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? "
            "AND p.image = 0 GROUP BY d", (subreddit, after, before))
        return {r["d"]: (r["whole"], r["n"]) for r in rows}

    def posts_by_ids(self, ids) -> list[sqlite3.Row]:
        ids = list(ids)
        return self._unpacked("posts", self.db.execute(f"SELECT * FROM posts WHERE id IN ({','.join('?' * len(ids))})", ids))

    def commit(self) -> None:
        self.db.commit()

    @contextmanager
    def snapshot(self):
        """Reads inside see the database as it was at the first one, whatever another connection saves meanwhile."""
        if self.db.in_transaction:  # already inside one, which sees a single state too
            yield
            return
        self.db.execute("BEGIN")
        try:
            yield
        finally:
            self.db.commit()

    def subreddits(self) -> list[str]:
        rows = self.db.execute(
            "SELECT subreddit, MAX(fetched_at) f FROM posts WHERE image = 0 GROUP BY subreddit COLLATE NOCASE "
            "ORDER BY f DESC")
        return [r["subreddit"] for r in rows]

    def datasets(self) -> list[dict]:
        """One entry per stored subreddit, most recently fetched first: name, posts, comments, last_fetched."""
        rows = self.db.execute(
            "SELECT subreddit, COUNT(*) n, MAX(fetched_at) f FROM posts WHERE image = 0 "
            "GROUP BY subreddit COLLATE NOCASE ORDER BY f DESC").fetchall()
        comments = {r["s"].lower(): r["n"] for r in self.db.execute(
            "SELECT c.subreddit s, COUNT(*) n FROM comments c WHERE " + NOT_IMAGE + " GROUP BY c.subreddit COLLATE NOCASE")}
        return [{"name": r["subreddit"], "posts": r["n"], "comments": comments.get(r["subreddit"].lower(), 0),
                 "last_fetched": r["f"]} for r in rows]

    def size_bytes(self) -> int:
        """Disk used by the database, including its WAL file."""
        return sum(p.stat().st_size for p in (self.path, self.path.with_name(self.path.name + "-wal"))
                   if p.exists())

    def posts(self, subreddit: str) -> list[Row]:
        """A subreddit's saved posts, newest first."""
        return self.rows("posts", subreddit)[::-1]

    def posts_since(self, subreddit: str, ts: float) -> list[Row]:
        """Posts saved at or after ``ts``, in the order they were saved: what a running fetch has added."""
        return self.rows("posts", subreddit, ts, words=True)

    def comments_since(self, subreddit: str, ts: float) -> list[Row]:
        return self.rows("comments", subreddit, ts, words=True)

    def comments(self, subreddit: str) -> list[Row]:
        return self.rows("comments", subreddit)[::-1]

    def notes(self, subreddit: str) -> dict[int, tuple[str, str]]:
        """A subreddit's timeline notes, as {day start: (label, description)}."""
        return {r["day"]: (r["label"], r["text"] or "") for r in self.db.execute(
            "SELECT day, label, text FROM notes WHERE subreddit = ?", (subreddit.lower(),))}

    def save_note(self, subreddit: str, day: int, label: str, text: str) -> None:
        """Set the note on a day, or remove it when the label and description are both empty."""
        if label or text:
            self.db.execute("INSERT OR REPLACE INTO notes VALUES (?, ?, ?, ?, ?)",
                            tuple(map(_storable, (subreddit.lower(), int(day), label, text, time.time()))))
        else:
            self.db.execute("DELETE FROM notes WHERE subreddit = ? AND day = ?", (subreddit.lower(), int(day)))
        self.db.commit()

    def words(self, subreddit: str) -> dict[str, float]:
        """The words and phrases set to mean something of their own in a subreddit, as {word: weight}."""
        return {r["word"]: r["weight"] for r in self.db.execute(
            "SELECT word, weight FROM words WHERE subreddit = ? ORDER BY word", (subreddit.lower(),))}

    def save_word(self, subreddit: str, word: str, weight: float | None) -> None:
        """Set what a word or phrase means in a subreddit, or with None let it mean what it usually does again."""
        word = Lexicon.key(word)
        if weight is None:
            self.db.execute("DELETE FROM words WHERE subreddit = ? AND word = ?", (subreddit.lower(), word))
        else:
            self.db.execute("INSERT OR REPLACE INTO words VALUES (?, ?, ?, ?)",
                            (subreddit.lower(), _storable(word), max(-1.0, min(1.0, float(weight))), time.time()))
        self.db.commit()
        self._lexicons.pop(subreddit.lower(), None)

    def lexicon(self, subreddit: str) -> Lexicon:
        """A subreddit's words as they're set now, for scoring what's saved there. They're read again once a
        second at most (or at once, set on this connection), so a fetch running on its own connection while the
        user sets one scores by it from then on without a query for each item."""
        hit = self._lexicons.get(subreddit.lower())
        if hit is None or abs(time.time() - hit[0]) > 1.0:
            words = self.words(subreddit)
            made = hit[2] if hit is not None and hit[1] == words else Lexicon(words)
            hit = self._lexicons[subreddit.lower()] = (time.time(), words, made)
        return hit[2]

    def rescore(self, kind: str, scores) -> None:
        """Save new scores, [(id, sentiment, label)], on posts or comments (``kind``), leaving the rest as it is.
        Like save_post, it's kept once committed. A packed row's pack still has its old score, so it's marked
        (packed 2) for its day to be packed again; till then RowIndex reads its numbers from the row."""
        self.db.executemany(f"UPDATE {kind} SET sentiment = ?, label = ?, packed = CASE packed WHEN 1 THEN 2 ELSE packed END "
                            "WHERE id = ?", [(s, label, i) for i, s, label in scores])

    def delete_subreddit(self, subreddit: str) -> None:
        for kind in PACKED:
            self.search.drop(kind, [i for (i,) in self.db.execute(
                f"SELECT id FROM {kind} WHERE subreddit = ? COLLATE NOCASE", (subreddit,))])
        self.db.execute("DELETE FROM posts WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comments WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comment_fetches WHERE post_id NOT IN (SELECT id FROM posts)")
        self.db.execute("DELETE FROM complete WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM removed_posts WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM notes WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM words WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM packs WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM dicts WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM vocab WHERE subreddit = ?", (subreddit.lower(),))
        self._vocab.pop(subreddit.lower(), None)
        self.db.commit()
        self._compact()

    def delete_days(self, subreddit: str, days) -> tuple[int, int]:
        """Forget what's saved from some UTC days (their starts) of a subreddit: the posts created on them, the
        comments on those posts, their text in the day packs, and every record of downloading them (complete
        days and threads, read-through spans, removed posts seen), so each day shows as never downloaded and
        a download reads it again in full. Timeline notes stay. One transaction. Returns the (posts, comments)
        deleted that the app shows (image posts and their comments are deleted too, but never shown)."""
        sub, days = subreddit.lower(), sorted({int(d) // 86400 * 86400 for d in days})
        if not days:
            return 0, 0
        self.db.commit()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.execute("CREATE TEMP TABLE gone (id TEXT PRIMARY KEY)")
            for d in days:
                self.db.execute("INSERT OR IGNORE INTO temp.gone SELECT id FROM posts WHERE subreddit = ? COLLATE NOCASE "
                                "AND created_utc >= ? AND created_utc < ?", (subreddit, d, d + 86400))
            shown = self.db.execute(
                "SELECT (SELECT COUNT(*) FROM posts WHERE image = 0 AND id IN temp.gone), (SELECT COUNT(*) FROM comments "
                "c WHERE c.post_id IN temp.gone AND " + NOT_IMAGE + ")").fetchone()
            # The comments' text sits in the packs of the days they were written, beside other posts' comments.
            repack: dict[int, set[str]] = {}
            for r in self.db.execute("SELECT id, CAST(created_utc / 86400 AS INTEGER) d FROM comments "
                                     "WHERE packed != 0 AND post_id IN temp.gone"):
                repack.setdefault(r["d"], set()).add(r["id"])
            self.search.drop("comments", [i for (i,) in self.db.execute("SELECT id FROM comments WHERE post_id IN temp.gone")])
            self.search.drop("posts", [i for (i,) in self.db.execute("SELECT id FROM temp.gone")])
            self.db.execute("DELETE FROM comments WHERE post_id IN temp.gone")
            self.db.execute("DELETE FROM comment_fetches WHERE post_id IN temp.gone")
            self.db.execute("DELETE FROM complete WHERE kind IN ('comments', 'comments_so_far') AND subreddit = ? "
                            "AND key IN temp.gone", (sub,))
            self.db.execute("DELETE FROM posts WHERE id IN temp.gone")
            self.db.executemany("DELETE FROM complete WHERE kind = 'posts' AND subreddit = ? AND key = ?",
                                [(sub, str(d)) for d in days])
            self.db.executemany("DELETE FROM removed_posts WHERE subreddit = ? AND created_utc >= ? AND created_utc < ?",
                                [(sub, d, d + 86400) for d in days])
            self.db.executemany("DELETE FROM packs WHERE kind = 'posts' AND subreddit = ? AND day = ?",
                                [(sub, d // 86400) for d in days])
            for day, ids in repack.items():
                self._write_pack("comments", sub, day, (), drop=ids)
            # What fetches read through, less the days deleted.
            spans = []
            for a, b in self.read_spans(subreddit):
                for d in days:
                    if a < d <= b:
                        spans.append((a, d - 1))
                    a = max(a, d + 86400) if a <= d + 86399 and b >= d else a
                if a <= b:
                    spans.append((a, b))
            self.db.execute("DELETE FROM complete WHERE kind = 'posts_read' AND subreddit = ?", (sub,))
            for a, b in spans:
                self.mark_complete("posts_read", subreddit, f"{a}:{b}")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.db.execute("DROP TABLE IF EXISTS temp.gone")
        self._compact()  # (a cached pack is let go when it's next read: it was changed or removed since)
        return shown[0], shown[1]

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM posts")
        self.db.execute("DELETE FROM comments")
        self.db.execute("DELETE FROM comment_fetches")
        self.db.execute("DELETE FROM complete")
        self.db.execute("DELETE FROM removed_posts")
        self.db.execute("DELETE FROM notes")
        self.db.execute("DELETE FROM words")
        self.db.execute("DELETE FROM packs")
        self.db.execute("DELETE FROM dicts")
        self.db.execute("DELETE FROM vocab")
        self.search.clear()
        self._vocab.clear()
        self.db.execute("DELETE FROM archive_counts")
        self.db.commit()
        self._compact()

    def import_from(self, path: str | Path) -> tuple[int, int]:
        """Merge another ikari database into this one: rows this one lacks are added, and a row both have
        keeps the copy fetched (or, for a timeline note or a word's meaning, saved) last. Nothing is removed from either file. Returns (new posts, new comments),
        image posts and their comments left out as they are everywhere.
        Raises sqlite3.DatabaseError if path isn't an ikari database, ValueError if it is this one,
        FileNotFoundError if there's no file there."""
        if not Path(path).is_file():  # ATTACH would create an empty file there
            raise FileNotFoundError(path)
        if Path(path).resolve() == self.path.resolve():
            raise ValueError("That is the database already open.")
        self.db.commit()
        self.db.execute("ATTACH DATABASE ? AS other", (str(path),))
        try:
            tables = {r[0] for r in self.db.execute("SELECT name FROM other.sqlite_master WHERE type = 'table'")}
            if not {"posts", "comments"} <= tables:
                raise sqlite3.DatabaseError("It has no saved posts or comments.")
            self.db.execute("CREATE TEMP TABLE new_posts AS SELECT id FROM other.posts "
                            "WHERE id NOT IN (SELECT id FROM main.posts)")
            self.db.execute("CREATE TEMP TABLE new_comments AS SELECT id FROM other.comments "
                            "WHERE id NOT IN (SELECT id FROM main.comments)")
            # A file saved before image posts were marked has no image column; its picture posts are found
            # the way Store.__init__ finds them, once they're in.
            flagged = "image" in {r["name"] for r in self.db.execute("PRAGMA other.table_info(posts)")}
            imported = [] if flagged else [r[0] for r in self.db.execute("SELECT id FROM other.posts")]
            post_columns = POST_COLUMNS + ("image",) if flagged else POST_COLUMNS
            # A file saved since packs existed has its settled days' text in its packs: the rows taken from it
            # come with their text, packed again here later (pack_settled).
            packed = "packs" in tables
            for table, key, columns in (("posts", "id", post_columns + ("packed",) * packed),
                                        ("comments", "id", COMMENT_COLUMNS + ("packed",) * packed),
                                        ("comment_fetches", "post_id", ("post_id", "ok", "settled", "fetched_at"))):
                if table in tables:
                    cols = ", ".join(columns)
                    self.db.execute(f"DROP TABLE IF EXISTS temp.taken_{table}")
                    self.db.execute(
                        f"CREATE TEMP TABLE taken_{table} AS SELECT o.{key} AS id FROM other.{table} o "
                        f"WHERE NOT EXISTS (SELECT 1 FROM main.{table} m WHERE m.{key} = o.{key} "
                        f"AND COALESCE(m.fetched_at, 0) >= COALESCE(o.fetched_at, 0))")
                    self.db.execute(f"INSERT OR REPLACE INTO main.{table} ({cols}) SELECT {cols} FROM other.{table} "
                                    f"WHERE {key} IN (SELECT id FROM temp.taken_{table})")
                    if table in PACKED and packed:
                        self._take_text(table)
                    if table in PACKED:  # their text, as it is now in their rows
                        self.search.put(table, [(r[0], r[1:]) for r in self.db.execute(
                            f"SELECT id, {', '.join(PACKED[table])} FROM main.{table} WHERE id IN temp.taken_{table}")])
            if "notes" in tables:  # a note on a day both have keeps the one saved last
                self.db.execute(
                    "INSERT OR REPLACE INTO main.notes (subreddit, day, label, text, saved_at) "
                    "SELECT subreddit, day, label, text, saved_at FROM other.notes o WHERE NOT EXISTS (SELECT 1 FROM "
                    "main.notes m WHERE m.subreddit = o.subreddit AND m.day = o.day "
                    "AND COALESCE(m.saved_at, 0) >= COALESCE(o.saved_at, 0))")
            if "words" in tables:  # likewise a word both have set in a subreddit
                self.db.execute(
                    "INSERT OR REPLACE INTO main.words (subreddit, word, weight, saved_at) "
                    "SELECT subreddit, word, weight, saved_at FROM other.words o WHERE NOT EXISTS (SELECT 1 FROM "
                    "main.words m WHERE m.subreddit = o.subreddit AND m.word = o.word "
                    "AND COALESCE(m.saved_at, 0) >= COALESCE(o.saved_at, 0))")
                self._lexicons.clear()
            for i in range(0, len(imported), 500):
                ids = imported[i:i + 500]
                self.db.executemany("UPDATE posts SET image = 1 WHERE id = ?", [
                    (r["id"],) for r in self.db.execute(
                        f"SELECT id, url, selftext FROM main.posts WHERE id IN ({','.join('?' * len(ids))})", ids)
                    if is_image_post(r["url"], r["selftext"])])
            for table, cols in (("complete", "kind, subreddit, key, fetched_at"),
                                ("removed_posts", "id, subreddit, created_utc")):
                if table in tables:
                    self.db.execute(f"INSERT OR IGNORE INTO main.{table} ({cols}) SELECT {cols} FROM other.{table}")
            new_posts = self.db.execute("SELECT COUNT(*) FROM main.posts WHERE image = 0 "
                                        "AND id IN (SELECT id FROM temp.new_posts)").fetchone()[0]
            new_comments = self.db.execute(f"SELECT COUNT(*) FROM main.comments c WHERE {NOT_IMAGE} "
                                           "AND c.id IN (SELECT id FROM temp.new_comments)").fetchone()[0]
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            for t in ("new_posts", "new_comments", "taken_posts", "taken_comments", "taken_comment_fetches"):
                self.db.execute(f"DROP TABLE IF EXISTS temp.{t}")
            self.db.execute("DETACH DATABASE other")
        return new_posts, new_comments

    def _take_text(self, table: str) -> None:
        """Fill in the text of the rows import_from just took packed from the attached file, from its packs."""
        cols = PACKED[table]
        days: dict[tuple, list[str]] = {}
        for r in self.db.execute(f"SELECT id, subreddit, created_utc FROM other.{table} WHERE packed != 0 "
                                 f"AND id IN (SELECT id FROM temp.taken_{table})"):
            days.setdefault((r["subreddit"].lower(), int(r["created_utc"] // 86400)), []).append(r["id"])
        for (sub, day), ids in days.items():
            blob = self.db.execute("SELECT data FROM other.packs WHERE kind = ? AND subreddit = ? AND day = ?",
                                   (table, sub, day)).fetchone()
            data = DayPack.read(blob[0], self.db, "other") if blob else {"columns": [], "rows": {}}
            self.db.executemany(
                f"UPDATE main.{table} SET packed = 0, {', '.join(c + ' = ?' for c in cols)} WHERE id = ?",
                [(*(dict(zip(data["columns"], data["rows"].get(i, ()))).get(c) for c in cols), i) for i in ids])

    def _compact(self) -> None:
        """Give deleted rows' space back to the OS, so deleting data actually frees disk."""
        try:
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:  # another connection is mid-write; space is reused later anyway
            pass

    def day_counts(self, subreddit: str) -> dict[int, tuple[int, int]]:
        """Saved posts and comments per UTC day, by when each was created, as {day start: (posts, comments)}."""
        days: dict[int, list[int]] = {}
        for i, (table, visible) in enumerate((("posts", "image = 0"),
                                              ("comments", NOT_IMAGE.replace("c.", "")))):
            for r in self.db.execute(
                    f"SELECT CAST(created_utc / 86400 AS INTEGER) * 86400 d, COUNT(*) n FROM {table} "
                    f"WHERE subreddit = ? COLLATE NOCASE AND created_utc IS NOT NULL AND {visible} GROUP BY d",
                    (subreddit,)):
                days.setdefault(r["d"], [0, 0])[i] = r["n"]
        return {d: (p, c) for d, (p, c) in days.items()}

    def export_csv(self, subreddit: str, kind: str, path: str | Path, days: set[int] | None = None,
                   min_score: int | None = None) -> int:
        """Write a subreddit's posts or comments to a CSV file: only those created on ``days`` (UTC day
        starts) when given, and only rows scoring ``min_score`` or more (see `scoring_at_least`). Returns how many rows were written."""
        rows = scoring_at_least(self.posts(subreddit) if kind == "posts" else self.comments(subreddit), min_score)
        if days is not None:
            rows = [r for r in rows if r["created_utc"] is not None and int(r["created_utc"] // 86400 * 86400) in days]
        columns = POST_COLUMNS if kind == "posts" else COMMENT_COLUMNS + ("post_title",)
        # Written beside the target and then swapped in, so a failure never leaves a half-written file.
        # The byte-order mark makes Excel on Windows read the file as UTF-8, not mangle emoji and accents.
        part = Path(str(path) + ".part")
        try:
            with open(part, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(columns)
                for r in rows:
                    w.writerow([_csv_cell(r[c]) for c in columns])
            os.replace(part, path)
        finally:
            part.unlink(missing_ok=True)
        return len(rows)
