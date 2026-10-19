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
CREATE INDEX IF NOT EXISTS posts_sub ON posts(subreddit COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS comments_sub ON comments(subreddit COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS comments_post ON comments(post_id);
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

    def comments(self, subreddit: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT c.*, p.title AS post_title FROM comments c LEFT JOIN posts p ON p.id = c.post_id "
            "WHERE c.subreddit = ? COLLATE NOCASE ORDER BY c.created_utc DESC",
            (subreddit,)).fetchall()

    def delete_subreddit(self, subreddit: str) -> None:
        self.db.execute("DELETE FROM posts WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.execute("DELETE FROM comments WHERE subreddit = ? COLLATE NOCASE", (subreddit,))
        self.db.commit()
        self._compact()

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM posts")
        self.db.execute("DELETE FROM comments")
        self.db.commit()
        self._compact()

    def _compact(self) -> None:
        """Give deleted rows' space back to the OS, so deleting data actually frees disk."""
        try:
            self.db.execute("VACUUM")
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.OperationalError:  # another connection is mid-write; space is reused later anyway
            pass

    def export_csv(self, subreddit: str, kind: str, path: str | Path) -> int:
        rows = self.posts(subreddit) if kind == "posts" else self.comments(subreddit)
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
