"""Where an ikari database's bytes go, and what each way of shrinking it would save.

Run from the repo root:  python tools/db_size.py <path to subsentiment.db>
                         python tools/db_size.py --build <new .db> [subreddit] [days] [posts]
                         python tools/db_size.py --migrate <path to subsentiment.db>
--build fetches from the real Arctic Shift archive into a new database (every comment on up to ``posts``
posts a day, default 300), as the app's Fetch does. --migrate opens a copy with this branch's Store and checks
every row reads back the same. The database measured is copied first; it is never changed.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

MB = 1024 * 1024


def build(path: Path, sub: str, days: int, posts: int) -> None:
    from subsentiment import collect
    from subsentiment.arctic_shift import ArcticShiftClient
    from subsentiment.store import Store

    store = Store(path)
    now = time.time()
    for d in range(days, 0, -1):
        start = (now // 86400 - d) * 86400
        req = collect.FetchRequest(sub, after=start, before=start + 86400, limit=posts, comments_per_post=0)
        print(collect.run(ArcticShiftClient(), store, req, progress=lambda *a: None), flush=True)
    store.close()


def file_size(path: Path) -> int:
    return sum(p.stat().st_size for p in (path, Path(str(path) + "-wal")) if p.exists())


def measure(path: Path) -> None:
    db = sqlite3.connect(path)
    page = db.execute("PRAGMA page_size").fetchone()[0]
    free = db.execute("PRAGMA freelist_count").fetchone()[0] * page
    wal = Path(str(path) + "-wal")
    print(f"file {path.stat().st_size / MB:.1f} MB, WAL {wal.stat().st_size / MB if wal.exists() else 0:.1f} MB, "
          f"free pages {free / MB:.1f} MB, page size {page}")
    print(f"rows: {db.execute('SELECT COUNT(*) FROM posts').fetchone()[0]} posts, "
          f"{db.execute('SELECT COUNT(*) FROM comments').fetchone()[0]} comments")
    print("\nby table and index (MB on disk, payload MB):")
    for name, size, payload in db.execute(
            "SELECT name, SUM(pgsize), SUM(payload) FROM dbstat GROUP BY name ORDER BY 2 DESC"):
        print(f"  {name:28} {size / MB:8.2f} {payload / MB:8.2f}")
    print("\nby column (MB of stored values; average bytes per row):")
    for table in ("posts", "comments"):
        n = db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 1
        for col in [r[1] for r in db.execute(f"PRAGMA table_info({table})")]:
            total = db.execute(f"SELECT SUM(length(CAST({col} AS BLOB))) FROM {table}").fetchone()[0] or 0
            print(f"  {table}.{col:14} {total / MB:8.2f}  {total / n:7.1f}")
    derivable = db.execute(
        "SELECT COUNT(*) FROM comments c JOIN posts p ON p.id = c.post_id "
        "WHERE c.permalink = p.permalink || c.id || '/'").fetchone()[0]
    print(f"\ncomment permalinks equal to the post's permalink + id: {derivable}")
    db.close()


def options(path: Path) -> None:
    """Apply each option to a copy and report the file size after a VACUUM."""
    tmp = Path(tempfile.mkdtemp())
    base = tmp / "base.db"
    shutil.copy(path, base)
    if Path(str(path) + "-wal").exists():
        shutil.copy(str(path) + "-wal", str(base) + "-wal")

    def trial(name: str, sql: str = "", page_size: int | None = None, fn=None) -> None:
        p = tmp / "trial.db"
        for suffix in ("", "-wal", "-shm"):
            Path(str(p) + suffix).unlink(missing_ok=True)
        shutil.copy(base, p)
        if Path(str(base) + "-wal").exists():
            shutil.copy(str(base) + "-wal", str(p) + "-wal")
        db = sqlite3.connect(p)
        db.execute("PRAGMA journal_mode=DELETE")
        if sql:
            db.executescript(sql)
        if fn:
            fn(db)
            db.commit()
        if page_size:
            db.execute(f"PRAGMA page_size={page_size}")
        t = time.time()
        db.execute("VACUUM")
        took = time.time() - t
        db.close()
        print(f"  {name:58} {file_size(p) / MB:8.1f} MB  (VACUUM {took:.1f} s)")

    print(f"\noptions, each on its own (now {file_size(path) / MB:.1f} MB with WAL):")
    trial("VACUUM only")
    trial("drop posts_sub, comments_sub (covered by *_fetched)",
          "DROP INDEX IF EXISTS posts_sub; DROP INDEX IF EXISTS comments_sub;")
    trial("comment permalink stored only when not derivable",
          "UPDATE comments SET permalink = NULL WHERE permalink = "
          "(SELECT p.permalink FROM posts p WHERE p.id = comments.post_id) || id || '/';")
    trial("drop comments.parent_id and depth (never read)",
          "UPDATE comments SET parent_id = NULL, depth = NULL;")
    for size in (8192, 16384):
        trial(f"page_size {size}", page_size=size)
    sample = [r[0] for r in sqlite3.connect(base).execute(
        "SELECT body FROM comments ORDER BY random() LIMIT 2000")]
    zdict = "\n".join(sample).encode()[-32768:]

    def compress(level: int, use_dict: bool):
        def run(db):
            for table, col in (("comments", "body"), ("posts", "selftext"), ("posts", "title")):
                rows = db.execute(f"SELECT rowid, {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
                out = []
                for rowid, text in rows:
                    data = text.encode()
                    if use_dict:
                        c = zlib.compressobj(level, zdict=zdict)
                    else:
                        c = zlib.compressobj(level)
                    z = c.compress(data) + c.flush()
                    out.append((z if len(z) < len(data) else text, rowid))
                db.executemany(f"UPDATE {table} SET {col} = ? WHERE rowid = ?", out)
        return run
    trial("zlib body/selftext/title (level 6, per row)", fn=compress(6, False))
    trial("zlib with a 32 KB shared dictionary", fn=compress(6, True))

    # What reading costs: loading every comment as the Comments table does, plain and decompressed.
    db = sqlite3.connect(base)
    t = time.time()
    rows = db.execute("SELECT * FROM comments").fetchall()
    plain = time.time() - t
    blobs = []
    for r in rows:
        c = zlib.compressobj(6, zdict=zdict)
        blobs.append(c.compress(r[5].encode()) + c.flush())
    t = time.time()
    for b in blobs:
        d = zlib.decompressobj(zdict=zdict)
        d.decompress(b).decode()
    print(f"\nloading {len(rows)} comments: {plain:.2f} s; decompressing their bodies adds {time.time() - t:.2f} s")
    shutil.rmtree(tmp)


def migrate(path: Path) -> None:
    from subsentiment.store import COMMENT_COLUMNS, POST_COLUMNS, Store, _unpack

    def contents(p: Path) -> dict:
        """Every row as an older ikari stored it: packed text filled back in, comment permalinks in full."""
        db = sqlite3.connect(p)
        db.row_factory = sqlite3.Row
        out = {t: sorted(map(tuple, db.execute(f"SELECT * FROM {t}")), key=repr)
               for t in ("comment_fetches", "complete", "removed_posts")}
        packs = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'packs'").fetchone():
            for kind, blob in db.execute("SELECT kind, data FROM packs"):
                data = _unpack(blob)
                for i, values in data["rows"].items():
                    packs[kind, i] = dict(zip(data["columns"], values))
        for t, cols in (("posts", POST_COLUMNS), ("comments", COMMENT_COLUMNS)):
            sql = f"SELECT {', '.join(cols)} FROM {t}" if t == "posts" else (
                "SELECT c.id, c.post_id, c.subreddit, c.parent_id, c.author, c.body, c.score, c.created_utc, "
                "c.depth, COALESCE(c.permalink, p.permalink || c.id || '/') AS permalink, c.sentiment, c.label, "
                "c.fetched_at FROM comments c LEFT JOIN posts p ON p.id = c.post_id")
            rows = [{**dict(r), **packs.get((t, r["id"]), {})} for r in db.execute(sql)]
            for r in rows if t == "comments" else ():  # a packed post's permalink is in its pack
                if r["permalink"] is None and ("posts", r["post_id"]) in packs:
                    r["permalink"] = packs["posts", r["post_id"]]["permalink"] + r["id"] + "/"
            out[t] = sorted((tuple(r[c] for c in cols) for r in rows), key=repr)
        db.close()
        return out

    tmp = Path(tempfile.mkdtemp())
    copy = tmp / "subsentiment.db"
    db, target = sqlite3.connect(f"file:{path}?mode=ro", uri=True), sqlite3.connect(copy)
    db.backup(target)  # with whatever the WAL file holds
    db.close()
    target.close()
    before, size = contents(copy), file_size(copy)
    t = time.time()
    Store(copy).close()  # what opening the window does before showing it
    opened = time.time() - t
    Store.upgrade(copy)  # then on a thread of its own
    took = time.time() - t - opened
    store = Store(copy)
    version = store.db.execute("PRAGMA user_version").fetchone()[0]
    check = store.db.execute("PRAGMA integrity_check").fetchone()[0]
    store.close()
    print(f"{size / MB:.1f} MB -> {file_size(copy) / MB:.1f} MB; opening {opened:.1f} s, packing {took:.1f} s; user_version {version}, "
          f"integrity {check}, every row reads back the same: {contents(copy) == before}, "
          f"files left: {sorted(p.name for p in tmp.iterdir())}")
    shutil.rmtree(tmp)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--migrate"]:
        migrate(Path(sys.argv[2]))
    elif sys.argv[1:2] == ["--build"]:
        build(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else "AskReddit",
              int(sys.argv[4]) if len(sys.argv) > 4 else 2, int(sys.argv[5]) if len(sys.argv) > 5 else 300)
    else:  # measured on a copy, so even the WAL file is left as it is
        tmp = Path(tempfile.mkdtemp()) / "copy.db"
        for suffix in ("", "-wal"):
            if Path(sys.argv[1] + suffix).exists():
                shutil.copy(sys.argv[1] + suffix, str(tmp) + suffix)
        measure(tmp)
        options(tmp)
        shutil.rmtree(tmp.parent)
