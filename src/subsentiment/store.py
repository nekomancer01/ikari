"""SQLite storage for fetched posts/comments and their sentiment scores."""
from __future__ import annotations

import csv
import json
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

import zstandard

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
-- compressed together: short texts barely shrink one by one, a day of them about 4x. data is zstd-compressed
-- JSON {"columns": [...], "rows": {id: [values]}}. See Store.pack_settled.
CREATE TABLE IF NOT EXISTS packs (
    kind TEXT NOT NULL, subreddit TEXT NOT NULL, day INTEGER NOT NULL, packed_at REAL, data BLOB NOT NULL,
    PRIMARY KEY (kind, subreddit, day)  -- subreddit lowercased
);
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
SCHEMA_VERSION = 2  # 1: derivable comment permalinks not stored, posts_sub and comments_sub indexes dropped
#                    2: settled days' text packed (packs table, packed columns)

# The columns kept in day packs: the text, which is most of what's saved. The rest stays in its row, so
# counts, filters and lookups by id still run in SQL. (A comment's permalink is mostly NULL already, rebuilt
# from its post's.)
PACKED = {"posts": ("title", "selftext", "author", "permalink", "url"), "comments": ("parent_id", "author", "body")}
SETTLE = 2 * 86400  # a day is packed once it's this old: scores have settled, so its rows rarely change
_ZSTD_LEVEL = 12


# Comments on image posts are hidden along with the posts (``c`` is the comments table).
NOT_IMAGE = "c.post_id NOT IN (SELECT id FROM posts WHERE image = 1)"


def scoring_at_least(rows, min_score: int | None) -> list:
    """The posts scoring at least ``min_score``, or the comments on them (a comment goes by its post's score,
    so it shows with its post); every row when None. With a filter on, a score Arctic Shift hasn't filled in
    yet (None, for about 36 hours) doesn't pass: those rows come back once it has. Always a new list."""
    rows = list(rows)
    if min_score is None or not rows:
        return rows
    col = "post_score" if "post_score" in rows[0].keys() else "score"
    return [r for r in rows if r[col] is not None and r[col] >= min_score]


def _unpack(blob: bytes) -> dict:
    return json.loads(zstandard.ZstdDecompressor().decompress(blob))


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
        """Pack every settled day of a database saved before packs existed, on its own connection. It can
        take seconds on a big one, so the app runs it on its own thread. A copy of the database is kept
        until every settled day is packed, across starts if it's cut short: each day is its own
        transaction, checked before it's committed (see pack_settled), so a day not reached is left as it
        was and packed next time."""
        self = cls(path)
        backup = Path(str(self.path) + "-before-v2")
        try:
            if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
                return
            if not backup.exists():  # one from a start that was cut short holds the database before any packing
                part = Path(str(backup) + "-part")
                copy = sqlite3.connect(part)
                try:
                    self.db.backup(copy)
                finally:
                    copy.close()
                os.replace(part, backup)
            self.pack_settled(vacuum=False, stop=stop)
            left = " + ".join(f"(SELECT COUNT(*) FROM {t} WHERE packed = 0 AND created_utc < ?)" for t in PACKED)
            if self.db.execute(f"SELECT {left}", (self._settled_before(),) * len(PACKED)).fetchone()[0]:
                return  # stopped, or some day couldn't be packed: carried on at the next start
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()
            self._compact()
            backup.unlink()
        except Exception:  # noqa: BLE001 - the database reads and saves as it is; the next start tries again
            pass
        finally:
            self.close()

    @staticmethod
    def _settled_before(now: float | None = None) -> float:
        """The start of the first day too recent to pack."""
        return ((now if now is not None else time.time()) - SETTLE) // 86400 * 86400

    def pack_settled(self, now: float | None = None, vacuum: bool = True, stop: threading.Event | None = None) -> int:
        """Pack the text of every settled day that has rows not packed yet: ones saved since the day was last
        packed, or never packed. Each day is one transaction: the new pack is checked to read back exactly the
        text it replaces before the rows' copies are cleared. Returns how many days were packed."""
        self.db.commit()
        before = self._settled_before(now)
        days: dict[tuple, list[str]] = {}  # (kind, subreddit, day) -> ids of its rows not packed yet
        for kind in PACKED:
            for i, sub, day in self.db.execute(
                    f"SELECT id, lower(subreddit), CAST(created_utc / 86400 AS INTEGER) FROM {kind} "
                    "WHERE packed = 0 AND created_utc < ?", (before,)):
                days.setdefault((kind, sub, day), []).append(i)
        packer = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
        done = 0
        for (kind, sub, day), ids in days.items():
            if stop is not None and stop.is_set():
                break
            cols = PACKED[kind]
            try:
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    # Read inside the lock, so a row saved again since is packed with its new text; one another
                    # connection packed meanwhile is skipped, its text being in the old pack read here.
                    text = dict(self._pack(kind, sub, day, cached=False))
                    for k in range(0, len(ids), 500):
                        chunk = ids[k:k + 500]
                        for r in self.db.execute(f"SELECT id, {', '.join(cols)} FROM {kind} WHERE packed = 0 AND "
                                                 f"id IN ({', '.join('?' * len(chunk))})", chunk):
                            text[r["id"]] = [r[c] for c in cols]
                    blob = packer.compress(json.dumps({"columns": cols, "rows": text}, ensure_ascii=False).encode())
                    if _unpack(blob) != {"columns": list(cols), "rows": text}:
                        raise sqlite3.DatabaseError(f"{kind} of r/{sub} on day {day} wouldn't read back the same")
                    self.db.execute("INSERT OR REPLACE INTO packs VALUES (?, ?, ?, ?, ?)", (kind, sub, day, time.time(), blob))
                    self.db.executemany(f"UPDATE {kind} SET packed = 1, {', '.join(c + ' = NULL' for c in cols)} "
                                        "WHERE id = ? AND packed = 0", [(i,) for i in ids])
                    self.db.commit()
                    done += 1
                except BaseException:
                    self.db.rollback()
                    raise
            except Exception:  # noqa: BLE001 - busy, or text JSON can't hold: leave the day as it is
                continue
        if done and vacuum:  # the cleared text leaves its pages part empty, which only a VACUUM gives back
            self._compact()
        return done

    def _pack(self, kind: str, sub: str, day: int, cached: bool = True) -> dict[str, list]:
        """One day's packed text, as {id: [values of PACKED[kind]]}; {} if there's no pack."""
        key = (kind, sub, day)
        row = self.db.execute("SELECT packed_at FROM packs WHERE kind = ? AND subreddit = ? AND day = ?", key).fetchone()
        if row is None:
            return {}
        with Store._packs_lock:
            hit = Store._packs.get((self.path, *key))
            if cached and hit and hit[0] == row[0]:
                Store._packs.move_to_end((self.path, *key))
                return hit[1]
        data = _unpack(self.db.execute("SELECT data FROM packs WHERE kind = ? AND subreddit = ? AND day = ?", key).fetchone()[0])
        rows = data["rows"] if data["columns"] == list(PACKED[kind]) else {  # packed with other columns
            i: [dict(zip(data["columns"], v)).get(c) for c in PACKED[kind]] for i, v in data["rows"].items()}
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

    def save_comment(self, c: Comment, sentiment: float, label: str) -> None:
        values = (c.id, c.post_id, c.subreddit, c.parent_id, c.author, c.body, c.score,
                  c.created_utc, c.depth, c.permalink, c.post_id, c.id, sentiment, label, self._saved_at())
        values = tuple(map(_storable, values))
        self.db.execute(f"INSERT OR REPLACE INTO comments ({', '.join(COMMENT_COLUMNS)}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
                        "NULLIF(?, (SELECT permalink FROM posts WHERE id = ?) || ? || '/'), ?, ?, ?)", values)

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
        its comments are saved) beside the post's own num_comments."""
        return self._unpacked("posts", self.db.execute(
            "SELECT p.id, p.subreddit, p.title, p.num_comments, p.score, p.created_utc, p.permalink, p.packed, "
            "(SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS saved_comments "
            "FROM posts p WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? "
            "AND p.image = 0 ORDER BY p.created_utc DESC", (subreddit, after, before)))

    def comment_counts(self, subreddit: str, after: float | None, before: float | None) -> dict[str, int]:
        """How many comments are saved on each post created in [after, before) that has any."""
        return dict(self.db.execute(
            "SELECT c.post_id, COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id "
            "WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? GROUP BY c.post_id",
            (subreddit, after if after is not None else float("-inf"),
             before if before is not None else float("inf"))).fetchall())

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

    def posts(self, subreddit: str) -> list[sqlite3.Row]:
        return self._unpacked("posts", self.db.execute(
            "SELECT * FROM posts WHERE subreddit = ? COLLATE NOCASE AND image = 0 ORDER BY created_utc DESC",
            (subreddit,)))

    def posts_since(self, subreddit: str, ts: float) -> list[sqlite3.Row]:
        """Posts saved at or after ``ts``, oldest first: what a running fetch has added."""
        return self._unpacked("posts", self.db.execute(
            "SELECT * FROM posts WHERE subreddit = ? COLLATE NOCASE AND fetched_at >= ? AND image = 0 "
            "ORDER BY fetched_at",
            (subreddit, ts)))

    def comments_since(self, subreddit: str, ts: float) -> list[sqlite3.Row]:
        return self._unpacked("comments", self.db.execute(
            _COMMENT_SELECT + " WHERE c.subreddit = ? COLLATE NOCASE AND c.fetched_at >= ? AND " + NOT_IMAGE + " ORDER BY c.fetched_at",
            (subreddit, ts)))

    def comments(self, subreddit: str) -> list[sqlite3.Row]:
        return self._unpacked("comments", self.db.execute(
            _COMMENT_SELECT + " WHERE c.subreddit = ? COLLATE NOCASE AND " + NOT_IMAGE + " ORDER BY c.created_utc DESC",
            (subreddit,)))

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
        Like save_post, it's kept once committed."""
        self.db.executemany(f"UPDATE {kind} SET sentiment = ?, label = ? WHERE id = ?",
                            [(s, label, i) for i, s, label in scores])

    def delete_subreddit(self, subreddit: str) -> None:
        self.db.execute("DELETE FROM posts WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comments WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comment_fetches WHERE post_id NOT IN (SELECT id FROM posts)")
        self.db.execute("DELETE FROM complete WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM removed_posts WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM notes WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM words WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM packs WHERE subreddit = ?", (subreddit.lower(),))
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
                                     "WHERE packed = 1 AND post_id IN temp.gone"):
                repack.setdefault(r["d"], set()).add(r["id"])
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
            packer = zstandard.ZstdCompressor(level=_ZSTD_LEVEL)
            for day, ids in repack.items():
                left = {i: v for i, v in self._pack("comments", sub, day, cached=False).items() if i not in ids}
                if left:
                    blob = packer.compress(json.dumps({"columns": PACKED["comments"], "rows": left},
                                                      ensure_ascii=False).encode())
                    if _unpack(blob) != {"columns": list(PACKED["comments"]), "rows": left}:  # as pack_settled checks
                        raise sqlite3.DatabaseError(f"comments of r/{sub} on day {day} wouldn't read back the same")
                    self.db.execute("UPDATE packs SET data = ?, packed_at = ? WHERE kind = 'comments' AND subreddit = ? "
                                    "AND day = ?", (blob, time.time(), sub, day))
                else:
                    self.db.execute("DELETE FROM packs WHERE kind = 'comments' AND subreddit = ? AND day = ?", (sub, day))
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
        for r in self.db.execute(f"SELECT id, subreddit, created_utc FROM other.{table} WHERE packed = 1 "
                                 f"AND id IN (SELECT id FROM temp.taken_{table})"):
            days.setdefault((r["subreddit"].lower(), int(r["created_utc"] // 86400)), []).append(r["id"])
        for (sub, day), ids in days.items():
            blob = self.db.execute("SELECT data FROM other.packs WHERE kind = ? AND subreddit = ? AND day = ?",
                                   (table, sub, day)).fetchone()
            data = _unpack(blob[0]) if blob else {"columns": [], "rows": {}}
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
        starts) when given, and only posts scoring ``min_score`` or more and their comments (see
        `scoring_at_least`). Returns how many rows were written."""
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
