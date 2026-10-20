"""How big an ikari database would be in other storage formats, and how fast each loads and grows.

Run from the repo root:  python tools/storage_bench.py <path to subsentiment.db>
Needs: pip install zstandard pyarrow duckdb. Works on a copy; the database given is never changed.
For each format it prints the size on disk, the time to load every comment into Python (what opening the
Comments table does), and the time and size cost of adding 1,000 comments the way a fetch does.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import zstandard

MB = 1024 * 1024
TEXT = {"posts": ("title", "selftext"), "comments": ("body",)}


def size(p: Path) -> int:
    if p.is_dir():
        return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return sum(Path(str(p) + s).stat().st_size for s in ("", "-wal") if Path(str(p) + s).exists())


def timed(fn):
    t = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t


def rows(db: sqlite3.Connection, table: str) -> list[dict]:
    cur = db.execute(f"SELECT * FROM {table}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur]


def report(name, path, load, append, before):
    print(f"  {name:44} {size(path) / MB:8.2f} MB   load {load:6.2f} s   "
          f"+1000 rows {append * 1000:7.1f} ms, +{(size(path) - before) / 1024:7.1f} KB", flush=True)


def main(src: Path) -> None:
    tmp = Path(tempfile.mkdtemp())
    base = tmp / "base.db"
    s, d = sqlite3.connect(f"file:{src}?mode=ro", uri=True), sqlite3.connect(base)
    s.backup(d)
    s.close()
    d.close()
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from subsentiment.store import Store
    Store(base).close()  # in the current layout, as the app would have it
    d = sqlite3.connect(base)
    d.execute("VACUUM")
    d.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    data = {t: rows(d, t) for t in ("posts", "comments")}
    d.close()
    T = "comments" if data["comments"] else "posts"  # the table loaded and added to: the bigger kind saved
    extra = [dict(c, id=f"zz{i}") for i, c in enumerate(data[T][:1000])]
    raw = sum(len(json.dumps(r).encode()) for t in data.values() for r in t)
    print(f"{len(data['posts'])} posts, {len(data['comments'])} comments; as JSON {raw / MB:.1f} MB")

    # 1. SQLite as ikari stores it now.
    p = tmp / "sqlite.db"
    shutil.copy(base, p)
    db = sqlite3.connect(p)
    _, load = timed(lambda: rows(db, T))
    before = size(p)
    cols = list(extra[0])
    _, append = timed(lambda: (db.executemany(f"INSERT INTO {T} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                              [tuple(r.values()) for r in extra]), db.commit()))
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.close()
    report("SQLite (now)", p, load, append, before)

    # 2. SQLite, text columns zstd-compressed per row with a dictionary trained on this data.
    samples = [(r[c] or "").encode() for t, cs in TEXT.items() for r in data[t] for c in cs if r[c]]
    zdict = zstandard.train_dictionary(110 * 1024, samples[:100000])
    for level in (3, 19):
        p = tmp / f"sqlite-zstd{level}.db"
        shutil.copy(base, p)
        db = sqlite3.connect(p)
        comp = zstandard.ZstdCompressor(level=level, dict_data=zdict)
        for t, cs in TEXT.items():
            for c in cs:
                db.executemany(f"UPDATE {t} SET {c} = ? WHERE rowid = ?",
                               [(comp.compress(v.encode()), rid) for rid, v in
                                db.execute(f"SELECT rowid, {c} FROM {t} WHERE {c} IS NOT NULL").fetchall()])
        db.commit()
        db.execute("VACUUM")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dec = zstandard.ZstdDecompressor(dict_data=zdict)

        def load_z():
            out = rows(db, T)
            for r in out:
                for c in TEXT[T]:
                    if r[c] is not None:
                        r[c] = dec.decompress(r[c]).decode()
            return out
        _, load = timed(load_z)
        before = size(p)
        _, append = timed(lambda: (db.executemany(
            f"INSERT INTO {T} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            [tuple(comp.compress(v.encode()) if k in TEXT[T] and v else v for k, v in r.items()) for r in extra]),
            db.commit()))
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        report(f"SQLite + zstd {level} text, 110 KB dictionary", p, load, append, before + len(zdict))

    # 3. Parquet, one file per table (appending means writing another file beside it).
    for level in (3, 19):
        p = tmp / f"parquet{level}"
        p.mkdir()
        for t in [t for t in ("posts", "comments") if data[t]]:
            pq.write_table(pa.Table.from_pylist(data[t]), p / f"{t}-0.parquet", compression="zstd",
                           compression_level=level)
        _, load = timed(lambda: pq.ParquetDataset([str(f) for f in p.glob(f"{T}-*.parquet")]).read().to_pylist())
        before = size(p)
        _, append = timed(lambda: pq.write_table(pa.Table.from_pylist(extra), p / f"{T}-1.parquet",
                                                 compression="zstd", compression_level=level))
        report(f"Parquet zstd {level}", p, load, append, before)

    # 4. DuckDB's own file format.
    p = tmp / "duck.db"
    con = duckdb.connect(str(p))
    for t in [t for t in ("posts", "comments") if data[t]]:
        arrow = pa.Table.from_pylist(data[t])
        con.register("src", arrow)
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src")
        con.unregister("src")
    con.execute("CHECKPOINT")
    _, load = timed(lambda: con.execute(f"SELECT * FROM {T}").to_arrow_table().to_pylist())
    before = size(p)
    arrow = pa.Table.from_pylist(extra)

    def dappend():
        con.register("src", arrow)
        con.execute(f"INSERT INTO {T} SELECT * FROM src")
        con.unregister("src")
        con.execute("CHECKPOINT")
    _, append = timed(dappend)
    con.close()
    report("DuckDB file", p, load, append, before)

    # 5. Day files: each table's rows as JSON lines in zstd frames, one frame per batch saved.
    for level in (3, 19):
        p = tmp / f"jsonl{level}"
        p.mkdir()
        comp = zstandard.ZstdCompressor(level=level, write_content_size=True)
        for t in ("posts", "comments"):
            (p / f"{t}.jsonl.zst").write_bytes(comp.compress("\n".join(json.dumps(r) for r in data[t]).encode()))
        _, load = timed(lambda: [json.loads(line) for line in zstandard.ZstdDecompressor().stream_reader(
            open(p / f"{T}.jsonl.zst", "rb"), read_across_frames=True).read().splitlines() if line])
        before = size(p)

        def jappend():
            with open(p / f"{T}.jsonl.zst", "ab") as f:  # another frame on the end
                f.write(comp.compress(("\n" + "\n".join(json.dumps(r) for r in extra)).encode()))
        _, append = timed(jappend)
        report(f"JSON lines, zstd {level}, one frame per save", p, load, append, before)

    # 6. Still one SQLite file, but each subreddit's rows of a UTC day are packed into one zstd block
    # (JSON, compressed with a dictionary so small days compress too). New rows are saved plainly, as
    # now, and packed into their day's block later; loading decompresses the blocks.
    for level in (3, 9, 19):
        p = tmp / f"packed{level}.db"
        db = sqlite3.connect(p)
        db.execute("CREATE TABLE packs (kind TEXT, subreddit TEXT, day INTEGER, data BLOB, "
                   "PRIMARY KEY (kind, subreddit, day))")
        pdict = zstandard.train_dictionary(110 * 1024, [json.dumps(r).encode() for r in data[T][:100000]])
        comp = zstandard.ZstdCompressor(level=level, dict_data=pdict)
        for t in ("posts", "comments"):
            days: dict = {}
            for r in data[t]:
                days.setdefault((r["subreddit"].lower(), int((r["created_utc"] or 0) // 86400)), []).append(r)
            db.executemany("INSERT INTO packs VALUES (?, ?, ?, ?)",
                           [(t, sub, day, comp.compress(json.dumps(rs).encode())) for (sub, day), rs in days.items()])
        db.commit()
        db.execute("VACUUM")
        dec = zstandard.ZstdDecompressor(dict_data=pdict)
        _, load = timed(lambda: [r for (b,) in db.execute("SELECT data FROM packs WHERE kind = ?", (T,))
                                 for r in json.loads(dec.decompress(b))])
        db.close()
        print(f"  {f'SQLite, day packs zstd {level} + dictionary':44} {(size(p) + len(pdict)) / MB:8.2f} MB   "
              f"load {load:6.2f} s   (new rows saved plainly until packed)", flush=True)

    # Reference: the whole SQLite file compressed, as a backup or download would be.
    for level in (3, 19):
        z = zstandard.ZstdCompressor(level=level).compress(base.read_bytes())
        print(f"  {'(the SQLite file zstd ' + str(level) + ', not usable live)':44} {len(z) / MB:8.2f} MB")
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
