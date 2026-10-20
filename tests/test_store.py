import csv

import pytest

from subsentiment import collect
from subsentiment.reddit import Comment, Post
from subsentiment.analysis import Analysis
from subsentiment.store import Store


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
