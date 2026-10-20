"""SQLite storage for fetched posts/comments and their sentiment scores."""
from __future__ import annotations

import csv
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from subsentiment.reddit import Comment, Post, is_image_post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    title TEXT, selftext TEXT, author TEXT,
    score INTEGER, upvote_ratio REAL, num_comments INTEGER,
    created_utc REAL, permalink TEXT, url TEXT,
    sentiment REAL, label TEXT,
    fetched_at REAL
    -- plus image: see Store.__init__, which adds it to databases saved before it existed
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    post_id TEXT NOT NULL,
    subreddit TEXT NOT NULL,
    parent_id TEXT, author TEXT, body TEXT,
    score INTEGER, created_utc REAL, depth INTEGER, permalink TEXT,
    sentiment REAL, label TEXT,
    fetched_at REAL
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
CREATE INDEX IF NOT EXISTS posts_sub ON posts(subreddit COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS comments_sub ON comments(subreddit COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id);
-- A running fetch's new rows are read back by when they were saved.
CREATE INDEX IF NOT EXISTS posts_fetched ON posts(subreddit COLLATE NOCASE, fetched_at);
CREATE INDEX IF NOT EXISTS comments_fetched ON comments(subreddit COLLATE NOCASE, fetched_at);
"""

POST_COLUMNS = ("id", "subreddit", "title", "selftext", "author", "score", "upvote_ratio",
                "num_comments", "created_utc", "permalink", "url", "sentiment", "label", "fetched_at")
COMMENT_COLUMNS = ("id", "post_id", "subreddit", "parent_id", "author", "body", "score",
                   "created_utc", "depth", "permalink", "sentiment", "label", "fetched_at")


# Comments on image posts are hidden along with the posts (``c`` is the comments table).
NOT_IMAGE = "c.post_id NOT IN (SELECT id FROM posts WHERE image = 1)"


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
        self.db.executescript(SCHEMA)
        # Image posts are kept as rows (so Download coverage counts them as checked and a refetch doesn't
        # download them again) but hidden from every read here and never given comments.
        if "image" not in {r["name"] for r in self.db.execute("PRAGMA table_info(posts)")}:
            self.db.execute("ALTER TABLE posts ADD COLUMN image INTEGER NOT NULL DEFAULT 0")
            self.db.executemany("UPDATE posts SET image = 1 WHERE id = ?", [
                (r["id"],) for r in self.db.execute("SELECT id, url, selftext FROM posts")
                if is_image_post(r["url"], r["selftext"])])
            self.db.commit()
        self._stamp = 0.0

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
        self.db.execute(f"INSERT OR REPLACE INTO posts ({','.join(POST_COLUMNS)}, image) "
                        f"VALUES ({','.join('?' * len(values))})", values)

    def save_comment(self, c: Comment, sentiment: float, label: str) -> None:
        values = (c.id, c.post_id, c.subreddit, c.parent_id, c.author, c.body, c.score,
                  c.created_utc, c.depth, c.permalink, sentiment, label, self._saved_at())
        values = tuple(map(_storable, values))
        self.db.execute(f"INSERT OR REPLACE INTO comments VALUES ({','.join('?' * len(values))})", values)

    def comment_ids(self, post_id: str) -> set[str]:
        """The ids of the comments saved on one post."""
        return {r[0] for r in self.db.execute("SELECT id FROM comments WHERE post_id = ?", (post_id,))}

    def posts_between(self, subreddit: str, after: float | None = None,
                      before: float | None = None) -> list[sqlite3.Row]:
        """Stored posts created in [after, before), each with has_comments and, when ikari has asked for
        its comments before, comments_ok and comments_settled. Image posts are included (image = 1): the
        caller needs them to skip, not to show."""
        return self.db.execute(
            "SELECT p.*, EXISTS(SELECT 1 FROM comments c WHERE c.post_id = p.id) AS has_comments, "
            "f.ok AS comments_ok, f.settled AS comments_settled, p.image "
            "FROM posts p LEFT JOIN comment_fetches f ON f.post_id = p.id WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ?",
            (subreddit, after if after is not None else float("-inf"),
             before if before is not None else float("inf"))).fetchall()

    def mark_comments(self, post_id: str, ok: bool, settled: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO comment_fetches VALUES (?, ?, ?, ?)",
                        (post_id, int(ok), int(settled), time.time()))

    def mark_complete(self, kind: str, subreddit: str, key: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO complete VALUES (?, ?, ?, ?)", (kind, subreddit.lower(), key, time.time()))

    def complete_keys(self, kind: str, subreddit: str) -> set[str]:
        return {r["key"] for r in self.db.execute(
            "SELECT key FROM complete WHERE kind = ? AND subreddit = ?", (kind, subreddit.lower()))}

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
        return self.db.execute(
            "SELECT p.id, p.subreddit, p.title, p.num_comments, p.score, p.created_utc, p.permalink, "
            "(SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS saved_comments "
            "FROM posts p WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? "
            "AND p.image = 0 ORDER BY p.created_utc DESC", (subreddit, after, before)).fetchall()

    def posts_by_ids(self, ids) -> list[sqlite3.Row]:
        ids = list(ids)
        return self.db.execute(f"SELECT * FROM posts WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()

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
        return self.db.execute(
            "SELECT * FROM posts WHERE subreddit = ? COLLATE NOCASE AND image = 0 ORDER BY created_utc DESC",
            (subreddit,)).fetchall()

    def posts_since(self, subreddit: str, ts: float) -> list[sqlite3.Row]:
        """Posts saved at or after ``ts``, oldest first: what a running fetch has added."""
        return self.db.execute(
            "SELECT * FROM posts WHERE subreddit = ? COLLATE NOCASE AND fetched_at >= ? AND image = 0 "
            "ORDER BY fetched_at",
            (subreddit, ts)).fetchall()

    def comments_since(self, subreddit: str, ts: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT c.*, p.title AS post_title FROM comments c LEFT JOIN posts p ON p.id = c.post_id "
            "WHERE c.subreddit = ? COLLATE NOCASE AND c.fetched_at >= ? AND " + NOT_IMAGE + " ORDER BY c.fetched_at",
            (subreddit, ts)).fetchall()

    def comments(self, subreddit: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT c.*, p.title AS post_title FROM comments c LEFT JOIN posts p ON p.id = c.post_id "
            "WHERE c.subreddit = ? COLLATE NOCASE AND " + NOT_IMAGE + " ORDER BY c.created_utc DESC",
            (subreddit,)).fetchall()

    def delete_subreddit(self, subreddit: str) -> None:
        self.db.execute("DELETE FROM posts WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comments WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comment_fetches WHERE post_id NOT IN (SELECT id FROM posts)")
        self.db.execute("DELETE FROM complete WHERE subreddit = ?", (subreddit.lower(),))
        self.db.execute("DELETE FROM removed_posts WHERE subreddit = ?", (subreddit.lower(),))
        self.db.commit()
        self._compact()

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM posts")
        self.db.execute("DELETE FROM comments")
        self.db.execute("DELETE FROM comment_fetches")
        self.db.execute("DELETE FROM complete")
        self.db.execute("DELETE FROM removed_posts")
        self.db.commit()
        self._compact()

    def import_from(self, path: str | Path) -> tuple[int, int]:
        """Merge another ikari database into this one: rows this one lacks are added, and a row both have
        keeps the copy fetched last. Nothing is removed from either file. Returns (new posts, new comments),
        image posts left out as they are everywhere.
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
            new_comments = self.db.execute("SELECT COUNT(*) FROM other.comments "
                                           "WHERE id NOT IN (SELECT id FROM main.comments)").fetchone()[0]
            # A file saved before image posts were marked has no image column; its picture posts are found
            # the way Store.__init__ finds them, once they're in.
            flagged = "image" in {r["name"] for r in self.db.execute("PRAGMA other.table_info(posts)")}
            imported = [] if flagged else [r[0] for r in self.db.execute("SELECT id FROM other.posts")]
            post_columns = POST_COLUMNS + ("image",) if flagged else POST_COLUMNS
            for table, key, columns in (("posts", "id", post_columns), ("comments", "id", COMMENT_COLUMNS),
                                        ("comment_fetches", "post_id", ("post_id", "ok", "settled", "fetched_at"))):
                if table in tables:
                    cols = ", ".join(columns)
                    self.db.execute(
                        f"INSERT OR REPLACE INTO main.{table} ({cols}) SELECT {cols} FROM other.{table} o "
                        f"WHERE NOT EXISTS (SELECT 1 FROM main.{table} m WHERE m.{key} = o.{key} "
                        f"AND COALESCE(m.fetched_at, 0) >= COALESCE(o.fetched_at, 0))")
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
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        finally:
            self.db.execute("DROP TABLE IF EXISTS temp.new_posts")
            self.db.execute("DETACH DATABASE other")
        return new_posts, new_comments

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

    def export_csv(self, subreddit: str, kind: str, path: str | Path, days: set[int] | None = None) -> int:
        """Write a subreddit's posts or comments to a CSV file: only those created on ``days`` (UTC day
        starts) when given. Returns how many rows were written."""
        rows = self.posts(subreddit) if kind == "posts" else self.comments(subreddit)
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
