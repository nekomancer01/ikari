import csv
import shutil
import sqlite3
from pathlib import Path

import pytest

from subsentiment import collect, store
from subsentiment.reddit import Comment, Post
from subsentiment.analysis import Analysis
from subsentiment.store import SCHEMA_VERSION, Store


def make_post(i, sub="Python", ts=1_700_000_000.0):
    return Post(f"p{i}", sub, f"title {i}", "", "a", i, 0.9, 1, ts, f"https://r/{i}", "")


def test_roundtrip_and_case_insensitive_lookup(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_post(make_post(1), 0.5, "positive")
    s.save_post(make_post(1), 0.6, "positive")  # re-fetch replaces
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi", 1, 1_700_000_100, 0, "u"), -0.3, "negative")
    s.commit()
    assert s.subreddits() == ["Python"]
    assert [r["sentiment"] for r in s.posts("python")] == [0.6]
    assert s.comments("PYTHON")[0]["post_title"] == "title 1"
    out = tmp_path / "c.csv"
    assert s.export_csv("python", "comments", out) == 1
    assert list(csv.DictReader(open(out)))[0]["body"] == "hi"
    s.delete_subreddit("python")
    assert s.subreddits() == []


def test_summary_and_daily_means():
    rows = [{"label": "positive", "sentiment": 0.8, "created_utc": 0},
            {"label": "negative", "sentiment": -0.4, "created_utc": 3600},
            {"label": "neutral", "sentiment": 0.0, "created_utc": 86400 + 5}]
    s = Analysis(rows)
    assert s.counts == {"negative": 1, "neutral": 1, "positive": 1}
    assert abs(s.mean - 0.4 / 3) < 1e-9
    assert s.daily_means == [(0, 0.2, 2), (86400, 0.0, 1)]


class FakeClient:
    def fetch_posts(self, *a, **kw):
        return [make_post(1, sub="AskScience"), make_post(2, sub="AskScience")]

    def fetch_comments(self, post, limit):
        return [Comment(f"c{post.id}", post.id, "AskScience", "", "b", "This is wonderful", 1, 1.0, 0, "u")]


def test_collect_run_scores_and_stores(tmp_path):
    s = Store(tmp_path / "x.db")
    req = collect.FetchRequest("askscience", include_comments=True)
    result = collect.run(FakeClient(), s, req)
    assert (result.subreddit, result.posts, result.comments) == ("AskScience", 2, 2)
    assert all(r["label"] == "positive" for r in s.comments("askscience"))


def test_datasets_summarise_each_subreddit_and_delete_all_empties_the_store(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_post(make_post(1), 0.5, "positive")
    s.save_post(make_post(2, sub="rust"), 0.1, "positive")
    s.save_comment(Comment("c1", "p1", "python", "t3_p1", "b", "hi", 1, 1_700_000_100, 0, "u"), 0.0, "neutral")
    s.commit()
    by_name = {d["name"]: d for d in s.datasets()}
    assert by_name["Python"]["posts"] == 1 and by_name["Python"]["comments"] == 1  # comment casing differs
    assert by_name["rust"]["comments"] == 0 and by_name["rust"]["last_fetched"]
    assert s.size_bytes() > 0
    s.delete_all()
    assert s.datasets() == [] and s.comments("python") == []


def test_whole_comment_threads_are_counted_per_day_the_way_the_downloader_leaves_them(tmp_path):
    from subsentiment.reddit import Comment, Post
    s = Store(tmp_path / "w.db")
    day = 1_791_936_000
    posts = {  # id: (num_comments, comments saved, complete mark): whole?
        "none": (0, 0, None),  # no comments: nothing to download
        "marked": (9, 2, "comments"),  # Download all read it to the end; the count includes removed ones
        "early": (None, 1, "comments_so_far"),  # read to the end before the archive counted it
        "counted": (4, 1, "comments_so_far"),  # counted since, and more than was saved
        "all": (2, 2, None),  # every comment the archive counts is saved
        "part": (3, 1, None),
        "never": (None, 0, None),
    }
    for i, (pid, (n, saved, mark)) in enumerate(posts.items()):
        s.save_post(Post(pid, "Python", "t", "", "a", 1, 0.9, n, day + 10 + i, "u", ""), 0.0, "neutral")
        for j in range(saved):
            s.save_comment(Comment(f"{pid}{j}", pid, "Python", "t3_" + pid, "b", "x", 1, day, 0, "u"), 0.0, "neutral")
        if mark:
            s.mark_complete(mark, "Python", pid)
    s.save_post(Post("next", "Python", "t", "", "a", 1, 0.9, 0, day + 86400 + 5, "u", ""), 0.0, "neutral")
    s.save_post(Post("pic", "Python", "t", "", "a", 1, 0.9, 0, day + 99, "u", "", image=True), None, None)  # hidden
    s.save_post(Post("other", "rust", "t", "", "a", 1, 0.9, 5, day + 50, "u", ""), 0.0, "neutral")
    s.mark_complete("comments", "Python", "other")  # a mark in another subreddit doesn't count
    s.commit()
    assert s.whole_threads_per_day("PYTHON", day, day + 2 * 86400) == {day: (4, 7), day + 86400: (1, 1)}
    assert s.whole_threads_per_day("rust", day, day + 86400) == {day: (0, 1)}
    s.save_post(Post("alone", "Go", "t", "", "a", 1, 0.9, None, day + 5, "u", ""), 0.0, "neutral")
    assert s.whole_threads_per_day("go", day, day + 86400) == {day: (0, 1)}  # a day of uncounted posts only
    # The Posts list offers comments for exactly the posts that aren't whole.
    assert {r["id"]: r["whole"] for r in s.comment_coverage("python", day, day + 86400)} == \
        {"none": 1, "marked": 1, "early": 1, "counted": 0, "all": 1, "part": 0, "never": 0}


def test_coverage_counts_per_day_and_complete_marks_are_cleared_with_the_data(tmp_path):
    from subsentiment.reddit import Comment, Post
    s = Store(tmp_path / "c.db")
    day = 1_791_936_000
    for i, t in enumerate((day + 10, day + 80000, day + 86400 + 5)):
        s.save_post(Post(f"p{i}", "Python", "t", "", "a", 1, 0.9, 4, t, "u", ""), 0.0, "neutral")
    s.save_comment(Comment("c1", "p0", "Python", "t3_p0", "b", "x", 1, day, 0, "u"), 0.0, "neutral")
    s.mark_complete("posts", "Python", str(day))
    s.mark_complete("comments", "rust", "r1")
    s.commit()
    assert s.posts_per_day("python", day, day + 2 * 86400) == {day: 2, day + 86400: 1}
    assert [(r["id"], r["saved_comments"], r["num_comments"]) for r in s.comment_coverage("python", day, day + 86400)] \
        == [("p1", 0, 4), ("p0", 1, 4)]
    assert s.complete_keys("posts", "PYTHON") == {str(day)}
    s.save_removed("Python", {"r0": day + 5, "r1": day + 86400 + 9, "p0": day + 10})  # p0 was saved first
    assert s.removed_per_day("python", day, day + 2 * 86400) == {day: 1, day + 86400: 1}
    s.delete_subreddit("python")
    assert s.removed_per_day("python", day, day + 2 * 86400) == {}
    assert s.complete_keys("posts", "python") == set() and s.complete_keys("comments", "rust") == {"r1"}
    s.delete_all()
    assert s.complete_keys("comments", "rust") == set()


def test_posts_and_comments_since_return_only_what_a_fetch_just_saved(tmp_path):
    import time
    s = Store(tmp_path / "x.db")
    p = Post("a", "Python", "t", "", "u", 1, None, 1, 1.0, "p", "")
    s.save_post(p, 0.0, "neutral")
    cut = time.time() + 0.01
    time.sleep(0.02)
    s.save_post(Post("b", "Python", "t", "", "u", 1, None, 1, 2.0, "p", ""), 0.0, "neutral")
    assert [r["id"] for r in s.posts_since("python", cut)] == ["b"]
    assert s.comments_since("python", cut) == []


def test_rows_saved_within_one_clock_tick_are_each_read_back_once(tmp_path, monkeypatch):
    # Windows' clock moves in 15.6 ms steps (Python 3.12), so a thread's comments saved together shared one
    # fetched_at, and each read of what the fetch added since the last row read got the whole batch again.
    import time
    import types
    from subsentiment import store as store_mod
    monkeypatch.setattr(store_mod, "time", types.SimpleNamespace(time=lambda: int(time.time() * 64) / 64))
    s = Store(tmp_path / "x.db")
    for i in range(50):
        s.save_comment(Comment(f"c{i}", "p", "Python", "t3_p", "u", "hi", 1, 1.0, 0, "x"), 0.0, "neutral")
    rows = s.comments_since("python", 0)
    assert len({r["fetched_at"] for r in rows}) == 50
    assert [r["id"] for r in s.comments_since("python", rows[-1]["fetched_at"])] == ["c49"]  # only the last


def test_text_sqlite_cant_store_is_saved_with_a_replacement_instead_of_failing(tmp_path):
    # The archive's JSON can hold half an emoji ("\ud83d", a title cut short); SQLite refuses it, which
    # failed the whole fetch with UnicodeEncodeError and saved nothing.
    s = Store(tmp_path / "x.db")
    s.save_post(Post("p1", "Python", "cut short \ud83d", "body \udc00", "a", 1, 0.9, 1, 1_700_000_000.0,
                     "https://r/1", ""), 0.1, "neutral")
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi \ud83d", 1, 1_700_000_100, 0, "u"),
                   0.0, "neutral")
    s.commit()
    assert s.posts("Python")[0]["title"] == "cut short ?"
    assert s.comments("Python")[0]["body"] == "hi ?"
    s.save_post(make_post(2), 0.5, "positive")  # ordinary text is stored as it is
    assert s.posts_by_ids(["p2"])[0]["title"] == "title 2"


def test_csv_opens_in_excel_as_utf8_and_cells_dont_run_as_formulas(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_post(Post("p1", "Python", '=HYPERLINK("http://x","y")', "", "a", -3, 0.9, 1, 1_700_000_000.0,
                     "https://r/1", ""), 0.1, "neutral")
    s.save_post(Post("p2", "Python", "café 😀, \"quoted\"\nnext line", "", "a", 2, 0.9, 1, 1_700_000_001.0,
                     "https://r/2", ""), 0.1, "neutral")
    s.commit()
    out = tmp_path / "p.csv"
    assert s.export_csv("Python", "posts", out) == 2
    raw = out.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # Excel on Windows reads it as UTF-8, not cp1252
    rows = {r["id"]: r for r in csv.DictReader(open(out, encoding="utf-8-sig", newline=""))}
    assert rows["p1"]["title"] == '\'=HYPERLINK("http://x","y")'
    assert rows["p1"]["score"] == "-3"  # numbers are left alone
    assert rows["p2"]["title"] == "café 😀, \"quoted\"\nnext line"


def test_a_failed_export_leaves_the_old_file_as_it_was(tmp_path, monkeypatch):
    s = Store(tmp_path / "x.db")
    s.save_post(make_post(1), 0.5, "positive")
    s.commit()
    out = tmp_path / "p.csv"
    out.write_text("earlier export")

    def refuse(src, dst):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    monkeypatch.setattr("subsentiment.store.os.replace", refuse)
    with pytest.raises(PermissionError):
        s.export_csv("Python", "posts", out)
    assert out.read_text() == "earlier export"
    assert list(tmp_path.glob("*.part")) == []


def test_day_counts_and_exporting_chosen_days(tmp_path):
    s = Store(tmp_path / "d.db")
    day = 1_700_006_400  # a UTC midnight
    s.save_post(make_post(1, ts=day + 5), 0.5, "positive")
    s.save_post(make_post(2, ts=day + 86400 + 5), 0.1, "neutral")
    s.save_post(make_post(3, sub="rust", ts=day + 5), 0.1, "neutral")
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi", 1, day + 86400 * 2, 0, "u"), 0.2, "positive")
    s.commit()
    # Comments count on the day they were written, not their post's day.
    assert s.day_counts("python") == {day: (1, 0), day + 86400: (1, 0), day + 2 * 86400: (0, 1)}
    out = tmp_path / "p.csv"
    assert s.export_csv("python", "posts", out, days={day + 86400}) == 1
    assert [r["id"] for r in csv.DictReader(open(out, encoding="utf-8-sig"))] == ["p2"]  # starts with a BOM
    assert s.export_csv("python", "posts", out) == 2
    assert s.export_csv("python", "comments", out, days=set()) == 0


POST_LINK = "https://www.reddit.com/r/Python/comments/p1/a_title/"


def test_a_comments_permalink_made_from_its_posts_is_rebuilt_when_read_not_stored(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_post(Post("p1", "Python", "t", "", "a", 1, 0.9, 2, 1_700_000_000, POST_LINK, ""), 0.0, "neutral")
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi", 1, 1_700_000_100, 0, POST_LINK + "c1/"), 0.0, "neutral")
    odd = "https://www.reddit.com/r/Python/comments/p1/_/c2/"
    s.save_comment(Comment("c2", "p1", "Python", "t3_p1", "b", "yo", 1, 1_700_000_200, 0, odd), 0.0, "neutral")
    s.commit()
    stored = dict(s.db.execute("SELECT id, permalink FROM comments"))
    assert stored == {"c1": None, "c2": odd}
    expected = {"c1": POST_LINK + "c1/", "c2": odd}
    assert {r["id"]: r["permalink"] for r in s.comments("python")} == expected
    assert {r["id"]: r["permalink"] for r in s.comments_since("python", 0)} == expected
    s.export_csv("python", "comments", tmp_path / "c.csv")
    assert {r["id"]: r["permalink"] for r in csv.DictReader(open(tmp_path / "c.csv", encoding="utf-8-sig"))} == expected


def test_a_comment_keeps_its_permalink_when_its_posts_changes(tmp_path):
    s = Store(tmp_path / "x.db")
    post = Post("p1", "Python", "t", "", "a", 1, 0.9, 2, 1_700_000_000, POST_LINK, "")
    s.save_post(post, 0.0, "neutral")
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi", 1, 1_700_000_100, 0, POST_LINK + "c1/"), 0.0, "neutral")
    s.save_post(post, 0.1, "neutral")  # saved again unchanged: still not stored
    assert s.db.execute("SELECT permalink FROM comments").fetchone()[0] is None
    post.permalink = "https://www.reddit.com/r/Python/comments/p1/"  # the archive's record lacked one
    s.save_post(post, 0.1, "neutral")
    s.commit()
    assert s.comments("python")[0]["permalink"] == POST_LINK + "c1/"


def test_no_index_repeats_the_start_of_another(tmp_path):
    s = Store(tmp_path / "x.db")
    indexes = {}
    for name, table in s.db.execute("SELECT name, tbl_name FROM sqlite_master WHERE type = 'index'"):
        columns = [(r["name"], r["coll"]) for r in s.db.execute(f"PRAGMA index_xinfo({name})") if r["key"]]
        indexes[name] = [table] + columns
    redundant = [(a, b) for a in indexes for b in indexes
                 if a != b and indexes[b][:len(indexes[a])] == indexes[a] and not a.startswith("sqlite_autoindex")]
    assert redundant == []


OLD_DB = Path(__file__).parent / "data" / "store-53829f7.db"  # saved by main at 53829f7, before compaction


def _contents(path):
    """Every row of every table as an older ikari reads it, comments with their full permalinks and packed
    text filled back in from its day's pack."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    out = {t: sorted(map(tuple, db.execute(f"SELECT * FROM {t}"))) for t in ("comment_fetches", "complete", "removed_posts")}
    packs = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name = 'packs'").fetchone():
        for r in db.execute("SELECT kind, data FROM packs"):
            data = store._unpack(r["data"])
            for i, values in data["rows"].items():
                packs[r["kind"], i] = dict(zip(data["columns"], values))
    for t, cols in (("posts", store.POST_COLUMNS), ("comments", store.COMMENT_COLUMNS)):
        sql = f"SELECT {', '.join(cols)} FROM {t}" if t == "posts" else (
            "SELECT c.id, c.post_id, c.subreddit, c.parent_id, c.author, c.body, c.score, c.created_utc, c.depth, "
            "COALESCE(c.permalink, p.permalink || c.id || '/') AS permalink, c.sentiment, c.label, c.fetched_at "
            "FROM comments c LEFT JOIN posts p ON p.id = c.post_id")
        rows = [{**dict(r), **packs.get((t, r["id"]), {})} for r in db.execute(sql)]
        if t == "comments":  # a packed post's permalink is in its pack
            for r in rows:
                if r["permalink"] is None and ("posts", r["post_id"]) in packs:
                    r["permalink"] = packs["posts", r["post_id"]]["permalink"] + r["id"] + "/"
        out[t] = sorted(tuple(r[c] for c in cols) for r in rows)
    db.close()
    return out


def test_a_database_from_an_older_ikari_is_shrunk_once_and_reads_back_the_same(tmp_path):
    path = tmp_path / "subsentiment.db"
    shutil.copy(OLD_DB, path)
    before = _contents(path)
    Store.upgrade(path)  # as the app's start does: Store(path) shrinks it, then its text is packed
    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert {"posts_sub", "comments_sub"}.isdisjoint(n for (n,) in s.db.execute("SELECT name FROM sqlite_master"))
    assert s.db.execute("SELECT COUNT(*) FROM comments WHERE permalink IS NULL").fetchone()[0] == 9
    assert s.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    s.close()
    assert _contents(path) == before
    assert list(tmp_path.iterdir()) == [path]  # its backup is gone once it worked
    db = sqlite3.connect(path)
    assert db.execute("SELECT COUNT(*) FROM posts WHERE packed = 0").fetchone()[0] == 0  # all from 2023: settled
    assert db.execute("SELECT COUNT(*) FROM comments WHERE body IS NOT NULL").fetchone()[0] == 0
    db.close()


WORDS = ("the game update finally fixed my save file and honestly I love the new music but the boss fight on "
         "chapter four is way too hard, anyone else think the devs should nerf it a little before the next patch").split()


def _chatter(rng, n):
    return " ".join(rng.choice(WORDS) for _ in range(n))


def test_settled_days_are_packed_smaller_and_read_back_the_same(tmp_path):
    import random
    rng = random.Random(4)
    s = Store(tmp_path / "x.db")
    day0 = 1_700_000_000 // 86400 * 86400
    for i in range(300):
        created = day0 + (i % 5) * 86400 + i
        link = f"https://www.reddit.com/r/Deltarune/comments/p{i}/{_chatter(rng, 4).replace(' ', '_')}/"
        s.save_post(Post(f"p{i}", "Deltarune", _chatter(rng, 8), _chatter(rng, rng.randint(0, 120)), f"user{rng.randint(1, 900)}",
                         5, 0.9, 10, created, link, link), 0.1, "positive")
        for j in range(10):
            s.save_comment(Comment(f"c{i}_{j}", f"p{i}", "Deltarune", f"t3_p{i}", f"user{rng.randint(1, 900)}",
                                   _chatter(rng, rng.randint(3, 40)), 1, created + 60 * j, 0, link + f"c{i}_{j}/"), 0.0, "neutral")
    s.commit()
    s._compact()
    before_rows = (s.posts("deltarune"), s.comments("deltarune"), s.posts_by_ids(["p7", "p8"]),
                   s.posts_between("deltarune", day0, day0 + 86400), s.comment_coverage("deltarune", day0, day0 + 86400))
    plain = s.size_bytes()
    assert s.pack_settled() == 10  # five days of posts and five of comments
    assert s.pack_settled() == 0  # nothing left to do
    assert s.size_bytes() < plain * 0.7  # the text is packed; ids, scores and indexes stay as rows
    strip = lambda rows: [{k: v for k, v in r.items() if k != "packed"} for r in rows]
    after = (s.posts("deltarune"), s.comments("deltarune"), s.posts_by_ids(["p7", "p8"]),
             s.posts_between("deltarune", day0, day0 + 86400), s.comment_coverage("deltarune", day0, day0 + 86400))
    for b, a in zip(before_rows, after):
        assert strip(a) == strip(b)


def test_a_row_saved_again_after_its_day_was_packed_reads_its_new_text_and_is_packed_again(tmp_path):
    s = Store(tmp_path / "x.db")
    post = Post("p1", "Python", "old title", "old text", "a", 1, 0.9, 2, 1_700_000_000, POST_LINK, "")
    s.save_post(post, 0.0, "neutral")
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi", 1, 1_700_000_100, 0, POST_LINK + "c1/"), 0.0, "neutral")
    s.commit()
    assert s.pack_settled() == 2
    assert s.comments("python")[0]["post_title"] == "old title"  # the post's title comes from its pack too
    post.title, post.selftext = "new title", "new text"
    s.save_post(post, 0.2, "positive")  # a score re-check saves it again
    s.save_comment(Comment("c2", "p1", "Python", "t3_p1", "b", "late reply", 1, 1_700_000_200, 0, POST_LINK + "c2/"), 0.0, "neutral")
    s.commit()
    assert [(r["title"], r["selftext"]) for r in s.posts("python")] == [("new title", "new text")]
    assert {r["id"]: r["body"] for r in s.comments("python")} == {"c1": "hi", "c2": "late reply"}
    assert s.pack_settled() == 2  # both days again, merged with what was packed
    assert s.db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == 2
    assert [(r["title"], r["selftext"]) for r in s.posts("python")] == [("new title", "new text")]
    assert {r["id"]: (r["body"], r["post_title"]) for r in s.comments("python")} == {
        "c1": ("hi", "new title"), "c2": ("late reply", "new title")}


def test_recent_days_stay_unpacked_and_deleting_a_subreddit_removes_its_packs(tmp_path):
    import time as clock
    s = Store(tmp_path / "x.db")
    s.save_post(Post("old", "Python", "t", "", "a", 1, 0.9, 2, 1_700_000_000, POST_LINK, ""), 0.0, "neutral")
    s.save_post(Post("new", "Python", "t", "", "a", 1, 0.9, 2, clock.time() - 3600, POST_LINK, ""), 0.0, "neutral")
    s.commit()
    assert s.pack_settled() == 1
    assert dict(s.db.execute("SELECT id, packed FROM posts")) == {"old": 1, "new": 0}
    s.delete_subreddit("python")
    assert s.db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == 0


def test_a_migration_that_cant_finish_leaves_the_database_as_it_was_and_runs_at_the_next_start(tmp_path, monkeypatch):
    path = tmp_path / "subsentiment.db"
    shutil.copy(OLD_DB, path)
    before = _contents(path)
    class FullDisk:  # the backup can't be written
        def __getattr__(self, name):
            return getattr(sqlite3, name)

        def connect(self, p, *a, **kw):
            if str(p).endswith("-before-v1"):
                raise sqlite3.OperationalError("database or disk is full")
            return sqlite3.connect(p, *a, **kw)
    monkeypatch.setattr(store, "sqlite3", FullDisk())
    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == 0
    assert len(s.comments("python")) == 11  # still reads, in the old layout
    s.close()
    assert _contents(path) == before
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM comments WHERE permalink IS NULL").fetchone()[0] == 0
    monkeypatch.undo()
    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == 0  # not retried by every fetch's store
    s.close()
    Store._migration_tried.clear()  # the next start
    Store.upgrade(path)
    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.close()
    assert _contents(path) == before


def test_packing_an_older_database_cut_short_keeps_its_backup_and_carries_on_at_the_next_start(tmp_path, monkeypatch):
    import threading
    path = tmp_path / "subsentiment.db"
    shutil.copy(OLD_DB, path)
    before = _contents(path)
    Store(path).close()  # the window's own store shrinks it first (version 1)
    shrunk = _contents(path)
    stop = threading.Event()
    stop.set()  # the app is closed before any day is packed
    Store.upgrade(path, stop)
    backup = tmp_path / "subsentiment.db-before-v2"
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name, backup.name]
    assert _contents(backup) == shrunk
    real = store.json.dumps
    days = []

    def fails_once(obj, **kw):  # a day whose text can't be packed is left as it is; the others still are
        days.append(obj)
        if len(days) == 1:
            raise TypeError("not JSON serializable")
        return real(obj, **kw)
    monkeypatch.setattr(store.json, "dumps", fails_once)
    Store.upgrade(path)
    monkeypatch.undo()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == len(days) - 1 > 0
    db.close()
    assert backup.exists() and _contents(backup) == shrunk  # still the copy from before any packing
    assert _contents(path) == before
    Store.upgrade(path)  # the next start
    assert list(tmp_path.iterdir()) == [path]
    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert s.db.execute("SELECT COUNT(*) FROM posts WHERE packed = 0").fetchone()[0] == 0
    s.close()
    assert _contents(path) == before


def test_a_migration_whose_result_doesnt_check_out_is_rolled_back(tmp_path, monkeypatch):
    path = tmp_path / "subsentiment.db"
    shutil.copy(OLD_DB, path)
    before = _contents(path)
    # Rebuilding permalinks wrongly must be caught by the check against the backup, not committed.
    monkeypatch.setattr(store, "_COMMENT_SELECT", store._COMMENT_SELECT.replace("|| c.id ||", "|| 'x' ||"))
    s = Store(path)
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == 0
    s.close()
    assert _contents(path) == before
    assert list(tmp_path.iterdir()) == [path]


def test_import_merges_another_database_keeping_the_copy_fetched_last(tmp_path, monkeypatch):
    import itertools
    import sqlite3
    import time as clock
    from subsentiment import store as store_mod
    when = itertools.count(100.0, 100.0)  # each save is fetched later than the one before
    monkeypatch.setattr(store_mod.time, "time", lambda: next(when))
    mine, theirs = Store(tmp_path / "mine.db"), Store(tmp_path / "theirs.db")
    mine.save_post(make_post(1), 0.1, "neutral")           # fetched at 100: older than theirs
    theirs.save_post(make_post(1), 0.9, "positive")        # 200
    theirs.save_post(make_post(2, sub="rust"), 0.5, "positive")  # 300, only theirs has it
    mine.save_post(make_post(3), -0.5, "negative")         # 400, only mine has it
    theirs.save_comment(Comment("c1", "p2", "rust", "t3_p2", "b", "hi", 1, 1_700_000_100, 0, "u"), 0.2, "positive")
    theirs.mark_complete("posts", "rust", "1699920000")
    theirs.save_removed("rust", {"gone": 1_700_000_000.0})
    mine.commit()
    theirs.commit()
    theirs.close()
    monkeypatch.setattr(store_mod.time, "time", clock.time)

    assert mine.import_from(tmp_path / "theirs.db") == (1, 1)
    assert {r["id"]: r["sentiment"] for r in mine.db.execute("SELECT * FROM posts")} == {"p1": 0.9, "p2": 0.5, "p3": -0.5}
    assert [r["body"] for r in mine.comments("rust")] == ["hi"]
    assert mine.complete_keys("posts", "rust") == {"1699920000"}
    assert mine.import_from(tmp_path / "theirs.db") == (0, 0)  # importing again adds nothing
    assert len(Store(tmp_path / "theirs.db").posts("rust")) == 1  # the imported file is left as it was

    (tmp_path / "junk.db").write_bytes(b"not a database at all" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        mine.import_from(tmp_path / "junk.db")
    with pytest.raises(ValueError):
        mine.import_from(tmp_path / "mine.db")
    with pytest.raises(FileNotFoundError):
        mine.import_from(tmp_path / "missing.db")
    assert not (tmp_path / "missing.db").exists()
    assert len(mine.subreddits()) == 2  # a failed import changes nothing and the store still works


def test_image_posts_are_hidden_from_every_read_and_counted_apart(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_post(make_post(1), 0.5, "positive")
    img = make_post(2)
    img.image = True
    s.save_post(img, None, None)
    s.save_comment(Comment("c1", "p1", "Python", "t3_p1", "b", "hi", 1, 1_700_000_100, 0, "u"), 0.1, "positive")
    s.save_comment(Comment("c2", "p2", "Python", "t3_p2", "b", "pretty", 1, 1_700_000_100, 0, "u"), 0.1, "positive")
    s.commit()
    day = int(1_700_000_000 // 86400 * 86400)
    assert [r["id"] for r in s.posts("python")] == ["p1"]
    assert [r["id"] for r in s.posts_since("python", 0)] == ["p1"]
    assert [r["id"] for r in s.comments("python")] == ["c1"]
    assert [r["id"] for r in s.comments_since("python", 0)] == ["c1"]
    assert s.datasets()[0]["posts"] == 1 and s.datasets()[0]["comments"] == 1
    assert s.day_counts("python") == {day: (1, 1)}
    assert s.posts_per_day("python", day, day + 86400) == {day: 1}
    assert s.posts_per_day("python", day, day + 86400, images=True) == {day: 1}
    assert [r["id"] for r in s.comment_coverage("python", day, day + 86400)] == ["p1"]
    assert {r["id"]: r["image"] for r in s.posts_between("python")} == {"p1": 0, "p2": 1}  # collect needs both
    assert s.export_csv("python", "posts", tmp_path / "p.csv") == 1
    assert s.export_csv("python", "comments", tmp_path / "c.csv") == 1


def test_posts_saved_before_images_were_filtered_are_hidden_on_open(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""CREATE TABLE posts (id TEXT PRIMARY KEY, subreddit TEXT NOT NULL, title TEXT, selftext TEXT,
        author TEXT, score INTEGER, upvote_ratio REAL, num_comments INTEGER, created_utc REAL, permalink TEXT,
        url TEXT, sentiment REAL, label TEXT, fetched_at REAL);
        INSERT INTO posts VALUES ('a', 'Python', 'pic', '', 'x', 1, 1, 0, 1700000000, '', 'https://i.redd.it/a.jpeg', 0, 'neutral', 1);
        INSERT INTO posts VALUES ('b', 'Python', 'pic with words', 'words', 'x', 1, 1, 0, 1700000000, '', 'https://i.redd.it/b.jpeg', 0, 'neutral', 1);
        INSERT INTO posts VALUES ('c', 'Python', 'text', '', 'x', 1, 1, 0, 1700000000, '', '', 0, 'neutral', 1);""")
    db.commit()
    db.close()
    s = Store(tmp_path / "old.db")
    assert sorted(r["id"] for r in s.posts("python")) == ["b", "c"]
    s.close()
    s = Store(tmp_path / "old.db")  # reopened: migrated once, not again
    assert sorted(r["id"] for r in s.posts("python")) == ["b", "c"]


def test_import_keeps_image_posts_hidden_from_files_old_and_new(tmp_path):
    import sqlite3
    old = sqlite3.connect(tmp_path / "old.db")  # saved before image posts were marked: no image column
    old.executescript("""CREATE TABLE posts (id TEXT PRIMARY KEY, subreddit TEXT NOT NULL, title TEXT, selftext TEXT,
        author TEXT, score INTEGER, upvote_ratio REAL, num_comments INTEGER, created_utc REAL, permalink TEXT,
        url TEXT, sentiment REAL, label TEXT, fetched_at REAL);
        CREATE TABLE comments (id TEXT PRIMARY KEY, post_id TEXT NOT NULL, subreddit TEXT NOT NULL, parent_id TEXT,
        author TEXT, body TEXT, score INTEGER, created_utc REAL, depth INTEGER, permalink TEXT, sentiment REAL,
        label TEXT, fetched_at REAL);
        INSERT INTO posts VALUES ('a', 'Python', 'pic', '', 'x', 1, 1, 0, 1700000000, '', 'https://i.redd.it/a.jpeg', 0, 'neutral', 1);
        INSERT INTO posts VALUES ('c', 'Python', 'text', '', 'x', 1, 1, 0, 1700000000, '', '', 0, 'neutral', 1);
        INSERT INTO comments VALUES ('k', 'a', 'Python', 't3_a', 'y', 'cute', 1, 1700000100, 0, '', 0, 'neutral', 1);""")
    old.commit()
    old.close()
    new = Store(tmp_path / "new.db")
    img = make_post(9, sub="rust")
    img.image = True
    new.save_post(img, None, None)
    new.save_post(make_post(8, sub="rust"), 0.5, "positive")
    new.commit()
    new.close()

    s = Store(tmp_path / "mine.db")
    assert s.import_from(tmp_path / "old.db") == (1, 0)  # the picture's comment comes in hidden with it
    assert s.import_from(tmp_path / "new.db") == (1, 0)
    assert [r["id"] for r in s.posts("python")] == ["c"]
    assert [r["id"] for r in s.posts("rust")] == ["p8"]
    assert "image" not in {r[1] for r in sqlite3.connect(tmp_path / "old.db").execute("PRAGMA table_info(posts)")}


@pytest.mark.parametrize("migrated", [False, True])
def test_importing_an_older_or_a_shrunk_database_keeps_every_comments_link(tmp_path, migrated):
    other = tmp_path / "other.db"
    shutil.copy(OLD_DB, other)
    if migrated:
        Store(other).close()
    s = Store(tmp_path / "mine.db")
    s.import_from(other)
    old = sqlite3.connect(OLD_DB)
    expected = dict(old.execute("SELECT id, permalink FROM comments"))
    old.close()
    assert {r["id"]: r["permalink"] for r in s.comments("python")} == expected


def test_timeline_notes_are_kept_per_subreddit_and_day_and_go_with_its_data(tmp_path):
    s = Store(tmp_path / "x.db")
    s.save_post(make_post(1), 0.5, "positive")
    s.save_note("Python", 86400 * 3, "UP", "Release day")
    s.save_note("python", 86400 * 5, "🎉", "")
    s.save_note("rust", 86400 * 3, "R", "Theirs")
    s.close()
    s = Store(tmp_path / "x.db")  # saved in the file, under the subreddit whatever its case
    assert s.notes("PYTHON") == {86400 * 3: ("UP", "Release day"), 86400 * 5: ("🎉", "")}
    s.save_note("python", 86400 * 3, "UP", "Release day, delayed")  # changing a day's note replaces it
    s.save_note("python", 86400 * 5, "", "")  # nothing left in it: removed
    assert s.notes("python") == {86400 * 3: ("UP", "Release day, delayed")}
    s.delete_subreddit("python")
    assert s.notes("python") == {} and s.notes("rust") == {86400 * 3: ("R", "Theirs")}
    s.delete_all()
    assert s.notes("rust") == {}


def test_import_brings_notes_keeping_the_one_saved_last(tmp_path, monkeypatch):
    from subsentiment import store as store_mod
    when = iter([100.0, 200.0, 300.0, 400.0])
    monkeypatch.setattr(store_mod.time, "time", lambda: next(when))
    mine, theirs = Store(tmp_path / "mine.db"), Store(tmp_path / "theirs.db")
    mine.save_note("python", 0, "A", "mine, older")       # 100
    theirs.save_note("python", 0, "B", "theirs, newer")   # 200
    theirs.save_note("python", 86400, "C", "only theirs")  # 300
    mine.save_note("python", 86400 * 2, "D", "only mine")  # 400
    theirs.close()
    monkeypatch.undo()
    mine.import_from(tmp_path / "theirs.db")
    assert mine.notes("python") == {0: ("B", "theirs, newer"), 86400: ("C", "only theirs"),
                                    86400 * 2: ("D", "only mine")}


def test_a_database_from_an_older_ikari_gets_notes_and_imports_without_them(tmp_path):
    path = tmp_path / "subsentiment.db"
    shutil.copy(OLD_DB, path)
    Store.upgrade(path)
    s = Store(path)  # migrated, and given the notes table
    assert s.db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    s.save_note("python", 0, "X", "kept")
    shutil.copy(OLD_DB, tmp_path / "old.db")
    s.import_from(tmp_path / "old.db")  # an older file has no notes table
    s.close()
    assert Store(path).notes("python") == {0: ("X", "kept")}


def test_score_filter_goes_by_the_rows_own_score(tmp_path):
    from subsentiment.store import scoring_at_least
    s = Store(tmp_path / "s.db")
    day = 1_700_006_400.0 // 86400 * 86400
    s.save_post(make_post(10, ts=day + 5), 0.5, "positive")  # scores 10
    s.save_comment(Comment("c1", "p10", "Python", "t3_p10", "b", "hi", -4, day + 86400, 0, "u"), 0.2, "positive")
    s.commit()
    posts, comments = s.posts("python"), s.comments("python")
    assert posts[0]["score"] == 10 and comments[0]["score"] == -4
    assert len(scoring_at_least(posts, 10)) == 1 and scoring_at_least(posts, 11) == []
    assert scoring_at_least(comments, 10) == []  # a comment goes by its own -4, not its post's 10
    assert len(scoring_at_least(comments, -5)) == 1 and len(scoring_at_least(comments, None)) == 1
    out = tmp_path / "c.csv"
    assert s.export_csv("python", "comments", out, min_score=0) == 0
    assert s.export_csv("python", "posts", out, days={int(day)}, min_score=10) == 1


def test_read_spans_merge_and_go_with_the_subreddit(tmp_path):
    s = Store(tmp_path / "a.db")
    s.save_read_spans("Python", [(100, 199), (300, 399)])
    s.save_read_spans("python", [(200, 250), (500, 599)])  # touches the first: one stretch
    assert s.read_spans("PYTHON") == [(100, 250), (300, 399), (500, 599)]
    assert s.read_spans("rust") == []
    s.delete_subreddit("Python")  # its posts are gone, so a download must read them again
    assert s.read_spans("python") == []
    s.close()


def test_word_meanings_are_kept_per_subreddit(tmp_path):
    s = Store(tmp_path / "w.db")
    s.save_word("Linux", "  Kill ", 0.0)
    s.save_word("linux", "big shot", 3)  # kept within -1..1
    s.save_word("Deltarune", "kill", -1)
    assert s.words("LINUX") == {"big shot": 1.0, "kill": 0.0}
    assert s.lexicon("linux").score("kill the process")[1] == "neutral"
    assert s.lexicon("deltarune").score("kill the process")[1] == "negative"
    s.save_word("linux", "kill", None)
    assert s.words("linux") == {"big shot": 1.0}
    assert s.lexicon("linux").score("kill the process")[1] == "negative"  # read again once changed
    s.delete_subreddit("Linux")
    assert s.words("linux") == {} and s.words("deltarune") == {"kill": -1.0}
    s.close()
    assert Store(tmp_path / "w.db").words("deltarune") == {"kill": -1.0}


def test_import_brings_word_meanings_keeping_the_one_saved_last(tmp_path, monkeypatch):
    from subsentiment import store as store_mod
    when = iter([100.0, 200.0, 300.0])
    monkeypatch.setattr(store_mod.time, "time", lambda: next(when))
    mine, theirs = Store(tmp_path / "mine.db"), Store(tmp_path / "theirs.db")
    mine.save_word("linux", "kill", -1.0)  # 100
    theirs.save_word("linux", "kill", 0.0)  # 200
    theirs.save_word("linux", "zombie", 0.5)  # 300
    theirs.close()
    monkeypatch.undo()
    assert mine.lexicon("linux").score("kill the process")[1] == "negative"
    mine.import_from(tmp_path / "theirs.db")
    assert mine.words("linux") == {"kill": 0.0, "zombie": 0.5}
    assert mine.lexicon("linux").score("kill the process")[1] == "neutral"


def test_deleting_days_forgets_them_whole_and_leaves_other_days_and_subreddits_as_they_were(tmp_path):
    s = Store(tmp_path / "x.db")
    d0 = 1_700_000_000 // 86400 * 86400
    days = [d0, d0 + 86400, d0 + 2 * 86400]

    def save_day(sub, k, day):
        s.save_post(Post(f"{sub}{k}", sub, f"title {sub}{k}", "text", "a", 1, 0.9, 2, day + 100, POST_LINK, ""), 0.1, "positive")
        # one reply the same day, and a late one written the next day, packed with that day's comments
        for j, at in enumerate((day + 200, day + 86400 + 300)):
            s.save_comment(Comment(f"c{sub}{k}_{j}", f"{sub}{k}", sub, f"t3_{sub}{k}", "b", f"reply {j}", 1, at, 0,
                                   POST_LINK + f"c{sub}{k}_{j}/"), 0.0, "neutral")
        s.mark_comments(f"{sub}{k}", True, True)
        s.mark_complete("comments", sub, f"{sub}{k}")
        s.mark_complete("posts", sub, str(day))
    for sub in ("Python", "rust"):
        for k, day in enumerate(days):
            save_day(sub, k, day)
    s.save_post(Post("img1", "Python", "pic", "", "a", 1, 0.9, 0, days[1] + 50, "https://i.redd.it/x.png",
                     "https://i.redd.it/x.png", image=True), 0.0, "neutral")
    s.save_removed("Python", {"gone1": days[1] + 10, "gone0": days[0] + 10})
    s.save_read_spans("Python", [(days[0], days[2] + 86399)])
    s.save_read_spans("rust", [(days[0], days[2] + 86399)])
    s.save_note("Python", days[1], "UP", "kept")
    s.commit()
    assert s.pack_settled() == 2 * (3 + 4)  # posts on three days, comments on those and the day after
    before = {sub: (s.posts(sub), s.comments(sub)) for sub in ("Python", "rust")}
    assert s.posts("python")[0]["title"] == "title Python2"  # its pack is in the cache now

    assert s.delete_days("python", [days[1] + 5000]) == (1, 2)  # any second of the day names it; the image post isn't shown

    posts, comments = before["Python"]
    assert s.posts("Python") == [r for r in posts if r["id"] != "Python1"]
    assert s.comments("Python") == [r for r in comments if r["post_id"] != "Python1"]
    assert (s.posts("rust"), s.comments("rust")) == before["rust"]
    assert s.posts_per_day("Python", days[0], days[2] + 86400, images=True) == {}
    assert s.removed_per_day("Python", days[0], days[2] + 86400) == {days[0]: 1}
    assert s.complete_keys("posts", "python") == {str(days[0]), str(days[2])}
    assert s.complete_keys("comments", "python") == {"Python0", "Python2"}
    assert len(s.complete_keys("posts", "rust")) == 3
    assert s.read_spans("python") == [(days[0], days[0] + 86399), (days[2], days[2] + 86399)]
    assert s.read_spans("rust") == [(days[0], days[2] + 86399)]
    assert s.db.execute("SELECT COUNT(*) FROM comment_fetches WHERE post_id = 'Python1'").fetchone()[0] == 0
    assert s.notes("python") == {days[1]: ("UP", "kept")}
    # The day's posts pack is gone, and the deleted comments are out of the packs they shared with others.
    packs = {(k, sub, day): store._unpack(blob)["rows"] for k, sub, day, blob in s.db.execute("SELECT kind, subreddit, day, data FROM packs")}
    assert ("posts", "python", days[1] // 86400) not in packs
    assert set(packs["comments", "python", days[2] // 86400]) == {"cPython2_0"}
    assert set(packs["comments", "python", days[1] // 86400]) == {"cPython0_1"}
    assert len(packs) == 2 * 7 - 1

    # They don't come back: not when the settled days are packed again, nor when the file is opened anew.
    assert s.pack_settled() == 0
    s.close()
    s = Store(tmp_path / "x.db")
    assert [r["id"] for r in s.posts("python")] == ["Python2", "Python0"]
    # A download of the day saves it again, with its new text read back (not an old cached pack's).
    s.save_post(Post("Python1", "Python", "downloaded again", "", "a", 1, 0.9, 2, days[1] + 100, POST_LINK, ""), 0.0, "neutral")
    s.commit()
    assert s.pack_settled() == 1
    assert {r["id"]: r["title"] for r in s.posts("python")}["Python1"] == "downloaded again"
    s.close()
