"""SQLite storage for fetched posts/comments and their sentiment scores."""
from __future__ import annotations

import csv
import sqlite3
import time
from pathlib import Path

from subsentiment.reddit import Comment, Post

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY,
    subreddit TEXT NOT NULL,
    title TEXT, selftext TEXT, author TEXT,
    score INTEGER, upvote_ratio REAL, num_comments INTEGER,
    created_utc REAL, permalink TEXT, url TEXT,
    sentiment REAL, label TEXT,
    fetched_at REAL
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


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")  # GUI reads while the fetch thread writes
        self.db.executescript(SCHEMA)

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
                  sentiment, label, time.time())
        self.db.execute(f"INSERT OR REPLACE INTO posts VALUES ({','.join('?' * len(values))})", values)

    def save_comment(self, c: Comment, sentiment: float, label: str) -> None:
        values = (c.id, c.post_id, c.subreddit, c.parent_id, c.author, c.body, c.score,
                  c.created_utc, c.depth, c.permalink, sentiment, label, time.time())
        self.db.execute(f"INSERT OR REPLACE INTO comments VALUES ({','.join('?' * len(values))})", values)

    def posts_between(self, subreddit: str, after: float | None = None,
                      before: float | None = None) -> list[sqlite3.Row]:
        """Stored posts created in [after, before), each with has_comments and, when ikari has asked for
        its comments before, comments_ok and comments_settled."""
        return self.db.execute(
            "SELECT p.*, EXISTS(SELECT 1 FROM comments c WHERE c.post_id = p.id) AS has_comments, "
            "f.ok AS comments_ok, f.settled AS comments_settled "
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

    def posts_per_day(self, subreddit: str, after: float, before: float) -> dict[int, int]:
        """Saved posts per UTC day in [after, before), as {day start: count}."""
        rows = self.db.execute(
            "SELECT CAST(created_utc / 86400 AS INTEGER) * 86400 d, COUNT(*) n FROM posts "
            "WHERE subreddit = ? COLLATE NOCASE AND created_utc >= ? AND created_utc < ? GROUP BY d",
            (subreddit, after, before))
        return {r["d"]: r["n"] for r in rows}

    def comment_coverage(self, subreddit: str, after: float, before: float) -> list[sqlite3.Row]:
        """Saved posts created in [after, before), newest first, each with saved_comments (how many of
        its comments are saved) beside the post's own num_comments."""
        return self.db.execute(
            "SELECT p.id, p.subreddit, p.title, p.num_comments, p.score, p.created_utc, p.permalink, "
            "(SELECT COUNT(*) FROM comments c WHERE c.post_id = p.id) AS saved_comments "
            "FROM posts p WHERE p.subreddit = ? COLLATE NOCASE AND p.created_utc >= ? AND p.created_utc < ? "
            "ORDER BY p.created_utc DESC", (subreddit, after, before)).fetchall()

    def posts_by_ids(self, ids) -> list[sqlite3.Row]:
        ids = list(ids)
        return self.db.execute(f"SELECT * FROM posts WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()

    def commit(self) -> None:
        self.db.commit()

    def subreddits(self) -> list[str]:
        rows = self.db.execute(
            "SELECT subreddit, MAX(fetched_at) f FROM posts GROUP BY subreddit COLLATE NOCASE ORDER BY f DESC")
        return [r["subreddit"] for r in rows]

    def datasets(self) -> list[dict]:
        """One entry per stored subreddit, most recently fetched first: name, posts, comments, last_fetched."""
        rows = self.db.execute(
            "SELECT subreddit, COUNT(*) n, MAX(fetched_at) f FROM posts "
            "GROUP BY subreddit COLLATE NOCASE ORDER BY f DESC").fetchall()
        comments = {r["s"].lower(): r["n"] for r in self.db.execute(
            "SELECT subreddit s, COUNT(*) n FROM comments GROUP BY subreddit COLLATE NOCASE")}
        return [{"name": r["subreddit"], "posts": r["n"], "comments": comments.get(r["subreddit"].lower(), 0),
                 "last_fetched": r["f"]} for r in rows]

    def size_bytes(self) -> int:
        """Disk used by the database, including its WAL file."""
        return sum(p.stat().st_size for p in (self.path, self.path.with_name(self.path.name + "-wal"))
                   if p.exists())

    def posts(self, subreddit: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM posts WHERE subreddit = ? COLLATE NOCASE ORDER BY created_utc DESC",
            (subreddit,)).fetchall()

    def posts_since(self, subreddit: str, ts: float) -> list[sqlite3.Row]:
        """Posts saved at or after ``ts``, oldest first: what a running fetch has added."""
        return self.db.execute(
            "SELECT * FROM posts WHERE subreddit = ? COLLATE NOCASE AND fetched_at >= ? ORDER BY fetched_at",
            (subreddit, ts)).fetchall()

    def comments_since(self, subreddit: str, ts: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT c.*, p.title AS post_title FROM comments c LEFT JOIN posts p ON p.id = c.post_id "
            "WHERE c.subreddit = ? COLLATE NOCASE AND c.fetched_at >= ? ORDER BY c.fetched_at",
            (subreddit, ts)).fetchall()

    def comments(self, subreddit: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT c.*, p.title AS post_title FROM comments c LEFT JOIN posts p ON p.id = c.post_id "
            "WHERE c.subreddit = ? COLLATE NOCASE ORDER BY c.created_utc DESC",
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
        for i, table in enumerate(("posts", "comments")):
            for r in self.db.execute(
                    f"SELECT CAST(created_utc / 86400 AS INTEGER) * 86400 d, COUNT(*) n FROM {table} "
                    "WHERE subreddit = ? COLLATE NOCASE AND created_utc IS NOT NULL GROUP BY d", (subreddit,)):
                days.setdefault(r["d"], [0, 0])[i] = r["n"]
        return {d: (p, c) for d, (p, c) in days.items()}

    def export_csv(self, subreddit: str, kind: str, path: str | Path, days: set[int] | None = None) -> int:
        """Write a subreddit's posts or comments to a CSV file: only those created on ``days`` (UTC day
        starts) when given. Returns how many rows were written."""
        rows = self.posts(subreddit) if kind == "posts" else self.comments(subreddit)
        if days is not None:
            rows = [r for r in rows if r["created_utc"] is not None and int(r["created_utc"] // 86400 * 86400) in days]
        columns = POST_COLUMNS if kind == "posts" else COMMENT_COLUMNS + ("post_title",)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(columns)
            for r in rows:
                w.writerow([r[c] for c in columns])
        return len(rows)


def summarize(rows) -> dict:
    """Counts per label and mean compound score for a set of post/comment rows."""
    counts = {"negative": 0, "neutral": 0, "positive": 0}
    total = 0.0
    for r in rows:
        counts[r["label"]] += 1
        total += r["sentiment"]
    n = sum(counts.values())
    return {"count": n, "counts": counts, "mean": total / n if n else 0.0}


def daily_means(rows) -> list[tuple[int, float, int]]:
    """[(day_start_utc, mean_sentiment, n)] sorted by day."""
    buckets: dict[int, list[float]] = {}
    for r in rows:
        day = int(r["created_utc"] // 86400 * 86400)
        buckets.setdefault(day, []).append(r["sentiment"])
    return [(d, sum(v) / len(v), len(v)) for d, v in sorted(buckets.items())]
