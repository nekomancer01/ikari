import random
import sqlite3
import time as clock

import numpy as np
import pytest

from subsentiment.analysis import Analysis, WordCounts
from subsentiment.index import STEPS, RowIndex, top_words
from subsentiment.reddit import Comment, Post
from subsentiment.store import PACKED, DayPack, Search, Store, scoring_at_least

WORDS = ("the game update finally fixed my save file and honestly I love the new music but the boss fight on "
         "chapter four is way too hard anyone else think the devs should nerf it a little before the next patch").split()
DAY0 = 1_690_000_000 // 86400 * 86400
SCORES = [None, 0, 1, 9, 10, 49, 50, 120, 600, 5000]
LABELS = ["positive", "neutral", "negative"]


def _fill(s, rng, posts=30, per=6, day0=DAY0, days=5, prefix=""):
    chat = lambda n: " ".join(rng.choice(WORDS) for _ in range(n))
    for i in range(posts):
        created = day0 + (i % days) * 86400 + i * 37
        link = f"https://www.reddit.com/r/Deltarune/comments/{prefix}p{i}/x/"
        s.save_post(Post(f"{prefix}p{i}", "Deltarune", chat(6), chat(rng.randint(0, 30)), f"u{i}", rng.choice(SCORES),
                         0.9, 8, created, link, link, image=i % 11 == 5), rng.uniform(-1, 1), LABELS[i % 3])
        for j in range(per):
            s.save_comment(Comment(f"{prefix}c{i}_{j}", f"{prefix}p{i}", "Deltarune", f"t3_{prefix}p{i}", f"v{j}",
                                   chat(rng.randint(0, 25)), rng.choice(SCORES), created + 30 * j, 0,
                                   link + f"{prefix}c{i}_{j}/"), rng.uniform(-1, 1), LABELS[(i + j) % 3])
    s.commit()


@pytest.fixture
def saved(tmp_path):
    """A subreddit with packed days, days saved again since, a rescored row and two days too new to pack."""
    path = tmp_path / "x.db"
    s = Store(path)
    rng = random.Random(3)
    _fill(s, rng)
    assert s.pack_settled() == 10
    _fill(s, rng, posts=6, per=3, day0=int(clock.time()) // 86400 * 86400 - 86400, days=2, prefix="new")
    _fill(s, rng, posts=3, per=2, day0=DAY0 + 7, prefix="late")  # saved after their days were packed
    s.rescore("comments", [("c4_2", 0.9, "positive"), ("c7_1", -0.9, "negative")])
    s.rescore("posts", [("p8", -0.5, "negative")])
    s.commit()
    assert s.db.execute("SELECT COUNT(*) FROM comments WHERE packed = 2").fetchone()[0] == 2
    yield path, s
    s.close()


def _same_rows(index, rows):
    assert [i.decode() for i in index.ids] == [r["id"] for r in rows]  # oldest first, created in the same order
    got = index.rows(range(len(index)))
    assert [tuple(g)[:-1] for g in got] == [tuple(r)[:-1] for r in rows]


@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_an_index_holds_every_row_as_store_reads_them(saved, kind):
    path, s = saved
    rows = s.rows(kind, "deltarune")
    index = RowIndex(path, kind, "Deltarune")
    _same_rows(index, rows)
    assert index.score.dtype == np.int32 and index.label.dtype == np.int8
    for i in (0, len(index) // 2, len(index) - 1):
        assert index.created[i] == rows[i]["created_utc"] and index.sentiment[i] == rows[i]["sentiment"]
    for min_score in (None, *STEPS, 7):
        expected, got = Analysis(scoring_at_least(rows, min_score)), index.analysis(min_score)
        assert got.counts == expected.counts
        assert got.total == pytest.approx(expected.total)
        assert {d: (v[1], v[2]) for d, v in got.days.items()} == {d: (v[1], v[2]) for d, v in expected.days.items()}
        assert {d: v[0] for d, v in got.days.items()} == pytest.approx({d: v[0] for d, v in expected.days.items()})
    index.close()


@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_word_counts_match_the_word_cloud_for_any_span_and_score(saved, kind):
    path, s = saved
    rows = s.rows(kind, "deltarune", words=True)
    index = RowIndex(path, kind, "Deltarune")
    days = (DAY0 + 86400, DAY0 + 3 * 86400)
    for span in (None, days):
        for min_score in (None, *STEPS):
            chosen = [r for r in scoring_at_least(rows, min_score)
                      if span is None or span[0] <= r["created_utc"] < span[1]]
            assert index.top_words(span, min_score, n=30) == WordCounts.top(WordCounts(kind, chosen), n=30)
    index.close()


def test_text_is_read_from_the_frames_and_a_rescored_row_keeps_its_place(saved):
    path, s = saved
    index = RowIndex(path, "comments", "deltarune")
    at = index.where(["c4_2", "newc0_0", "latec1_1", "nope"])
    assert at[-1] == -1
    rescored, new, late = at[:3]
    assert index._pack_of[rescored] >= 0  # its text is still in its pack's frame
    assert index._pack_of[new] < 0 and index._pack_of[late] < 0  # their text is in their rows
    assert index.text(new) is None
    body = s.db.execute("SELECT body FROM comments WHERE id = 'c4_2'").fetchone()[0]
    assert body is None and index.text(rescored)[PACKED["comments"].index("body")] == s.rows("comments", "deltarune")[rescored]["body"]
    assert index.label[rescored] == 2 and index.sentiment[rescored] == 0.9
    index.close()


def test_rows_a_fetch_saves_and_rescores_are_counted_in(saved):
    path, s = saved
    index = RowIndex(path, "comments", "deltarune")
    before = len(index)
    since = clock.time()
    clock.sleep(0.01)
    rng = random.Random(9)
    _fill(s, rng, posts=4, per=3, day0=DAY0 + 11, prefix="fresh")  # new rows on old days
    s.save_comment(Comment("c3_0", "p3", "Deltarune", "t3_p3", "v0", "edited text", 77, DAY0 + 3 * 86400 + 113, 0,
                           "https://www.reddit.com/r/Deltarune/comments/p3/x/c3_0/"), -0.2, "negative")
    s.commit()
    kept = index.ids.copy()
    index.add(s.rows("comments", "deltarune", since=since))
    assert list(index.ids[:before]) == list(kept)  # every row keeps its position; the new ones go at the end
    assert index.ordered == before  # and aren't in time order
    days = (DAY0, DAY0 + 86400)
    s.rescore("comments", [("c9_3", 0.7, "positive")])
    s.commit()
    index.rescore([("c9_3", 0.7, "positive")])
    assert len(index) == before + 12
    rows = s.rows("comments", "deltarune", words=True)
    in_day = {r["id"] for r in rows if days[0] <= r["created_utc"] < days[1]}
    assert {i.decode() for i in index.ids[index.mask(days)]} == in_day
    unordered = index.ids.copy()
    order = index.reorder()
    assert list(index.ids) == list(unordered[order])  # each new position's old one, to remap
    _same_rows(index, rows)
    assert {i.decode() for i in index.ids[index.mask(days)]} == in_day
    assert index.reorder().tolist() == list(range(len(index)))  # in order already
    assert Analysis(rows).counts == index.analysis().counts
    assert index.top_words(n=20) == WordCounts.top(WordCounts("comments", rows), n=20)
    index.close()


def test_a_pack_from_before_frames_is_read_until_it_is_packed_again(tmp_path):
    """Rows of a pack saved by 0.4 (no column block yet) are read from their rows' numbers and its text."""
    path = tmp_path / "x.db"
    s = Store(path)
    _fill(s, random.Random(5), posts=10, per=3)
    s.pack_settled()
    rows = s.rows("comments", "deltarune")
    import json
    import zstandard
    for kind, sub, day, blob in list(s.db.execute("SELECT kind, subreddit, day, data FROM packs")):
        old = DayPack.read(blob, s.db)  # as 0.4 packed it: JSON, no column block
        s.db.execute("UPDATE packs SET data = ?, cols = NULL, words = NULL WHERE kind = ? AND subreddit = ? AND day = ?",
                     (zstandard.ZstdCompressor().compress(json.dumps(old).encode()), kind, sub, day))
    s.commit()
    index = RowIndex(path, "comments", "deltarune")
    _same_rows(index, rows)
    assert Analysis(rows).counts == index.analysis().counts
    index.close()
    assert s.pack_settled() == 0  # a fetch leaves them to Store.upgrade
    assert s.pack_settled(old=True) == 10  # packed again, with its column block
    assert s.db.execute("SELECT COUNT(*) FROM packs WHERE cols IS NULL").fetchone()[0] == 0
    _same_rows(RowIndex(path, "comments", "deltarune"), rows)


def test_an_empty_subreddit_has_an_empty_index(tmp_path):
    Store(tmp_path / "x.db").close()
    index = RowIndex(tmp_path / "x.db", "comments", "nothing")
    assert len(index) == 0 and index.analysis().counts == Analysis().counts and index.top_words() == []
    assert list(index.where(["a"])) == [-1] and index.rows([]) == []
    index.close()


def test_deleting_a_day_takes_out_its_comments_saved_again_since_they_were_packed(tmp_path):
    """A refetch saves settled comments again (unpacked, their old copy still in the day's pack); deleting the
    day before they're packed again left that copy in the pack, so the deleted day kept showing them and stayed
    downloaded."""
    s = Store(tmp_path / "x.db")
    link = "https://www.reddit.com/r/Foo/comments/p1/t/"
    s.save_post(Post("p1", "Foo", "title", "text", "a", 1, 0.9, 3, DAY0 + 100, link, ""), 0.5, "positive")
    comments = [Comment(f"c{i}", "p1", "Foo", "t3_p1", "b", "hello world", 1, DAY0 + 200 + i, 0, f"{link}c{i}/")
                for i in range(3)]
    s.save_comments(comments, [(0.5, "positive")] * 3)
    s.commit()
    assert s.pack_settled() == 2
    s.save_comments(comments, [(0.5, "positive")] * 3)
    s.commit()
    assert s.delete_days("foo", [DAY0]) == (1, 3)
    assert s.db.execute("SELECT COUNT(*) FROM packs").fetchone()[0] == 0
    assert s.downloaded_days("Foo") == []
    index = RowIndex(s.path, "comments", "Foo")
    assert len(index) == 0 and index.top_words() == []
    index.close()


@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_days_packed_again_count_only_what_changed_and_still_match(saved, kind):
    """Packing a day again takes out what its changed rows counted and adds what they count now."""
    path, s = saved
    s.save_comment(Comment("c2_1", "p2", "Deltarune", "t3_p2", "v1", "brand new words zebra quokka", 600,
                           DAY0 + 2 * 86400 + 74 + 30, 0, "https://www.reddit.com/r/Deltarune/comments/p2/x/c2_1/"),
                   0.3, "positive")
    s.commit()
    s.pack_settled()
    s.delete_days("deltarune", [DAY0 + 86400])
    s.rescore(kind, [(r["id"], -0.8, "negative") for r in s.rows(kind, "deltarune")[::4]])
    s.commit()
    s.pack_settled()
    assert s.db.execute(f"SELECT COUNT(*) FROM {kind} WHERE packed = 2").fetchone()[0] == 0
    rows = s.rows(kind, "deltarune", words=True)
    index = RowIndex(path, kind, "deltarune")
    _same_rows(index, rows)
    for min_score in (None, 10, 500):
        chosen = scoring_at_least(rows, min_score)
        assert index.top_words(None, min_score, n=40) == WordCounts.top(WordCounts(kind, chosen), n=40)
    words = {w: n for w, n, _ in index.top_words(n=10_000)}
    assert ("zebra" in words) == (kind == "comments")
    index.close()


@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_rescoring_with_the_words_split_elsewhere_moves_them_without_reading_any_text(saved, kind, monkeypatch):
    """A word's meaning set on a big subreddit rescored tens of thousands of packed rows, and each one's text was
    read from its pack and split again on the window's thread (17 s of 40 janky ones). The rescore's own worker
    processes split it now: the counts come out as a fresh index has them, and no text is read."""
    from subsentiment.analysis import words_in
    from subsentiment.sentiment import Lexicon

    path, s = saved
    index = RowIndex(path, kind, "deltarune")
    rows = s.rows(kind, "deltarune")[::3] + [{"id": "nope", "title": "", "selftext": "", "body": ""}]
    texts = [(r["body"] or "") if kind == "comments" else f"{r['title'] or ''}\n\n{r['selftext'] or ''}".strip()
             for r in rows]
    got = Lexicon.compare({"boss": -1.0}, frozenset({"boss"}), texts, split=True)
    assert [g[3] for g in got] == [words_in(t) for t in texts]
    scores = [(r["id"], score, label) for r, (score, label, *_) in zip(rows, got)]
    s.rescore(kind, scores)
    s.commit()
    monkeypatch.setattr(RowIndex, "text", lambda *a, **k: pytest.fail("text read again"))
    at = index.rescore(scores, [g[3] for g in got])
    assert at[-1] == -1 and [index.ids[a].decode() for a in at[:-1]] == [r["id"] for r in rows[:-1]]
    monkeypatch.undo()
    fresh = RowIndex(path, kind, "deltarune")
    assert [a.tolist() for a in index.word_counts()] == [a.tolist() for a in fresh.word_counts()]
    assert index.label.tolist() == fresh.label.tolist() and index.sentiment.tolist() == fresh.sentiment.tolist()
    index.close()
    fresh.close()


def test_an_id_longer_than_every_saved_one_is_not_mistaken_for_one_it_starts_with(saved):
    path, s = saved
    index = RowIndex(path, "posts", "deltarune")
    p1 = int(index.where(["p1"])[0])
    assert p1 >= 0 and list(index.where(["p1", "p1xyzzyq", b"p1"])) == [p1, -1, p1]
    index.close()


def test_rows_read_after_their_day_was_packed_still_have_their_text(saved):
    path, s = saved
    import time as clock
    since = clock.time()
    s.save_comment(Comment("c6_1", "p6", "Deltarune", "t3_p6", "v1", "saved again during a fetch", 3,
                           DAY0 + 86400 + 6 * 37 + 30, 0, "https://www.reddit.com/r/Deltarune/comments/p6/x/c6_1/"), 0.1, "positive")
    s.commit()
    index = RowIndex(path, "comments", "deltarune")
    index.add(s.rows("comments", "deltarune", since=since))
    assert index._pack_of[index.where(["c6_1"])[0]] < 0
    s.pack_settled()  # as the fetch ends: the rows read one by one are packed now
    expected = s.rows("comments", "deltarune")
    assert [tuple(r)[:-1] for r in index.rows(range(len(index)))] == [tuple(r)[:-1] for r in expected]
    assert {r.id: r.body for r in index.rows(index.where(["c6_1"]))} == {"c6_1": "saved again during a fetch"}
    index.close()


@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_a_text_column_reads_as_the_rows_have_it_a_few_rows_at_a_time(saved, kind):
    path, s = saved
    rows = s.rows(kind, "deltarune")
    index = RowIndex(path, kind, "deltarune")
    # The packed text, and columns kept in the rows (a table sorts posts by their comment count).
    names = list(PACKED[kind]) + (["post_title"] if kind == "comments" else ["num_comments"]) + ["score"]
    for name in names:
        whole = [r[name] for r in rows]
        assert index.column(name) == whole, name
        assert [v for k in range(0, len(index), 7) for v in index.column(name, k, k + 7)] == whole, name
    with pytest.raises(ValueError):
        index.column("no such column")
    with pytest.raises(ValueError):
        index.column("post_title" if kind == "posts" else "num_comments")
    if kind == "comments":
        with pytest.raises(ValueError):  # rebuilt from its post's as it's read
            index.column("permalink")
    index.close()


def test_a_word_cloud_of_posts_and_comments_together_each_with_its_own_score_filter(saved):
    path, s = saved
    s.save_comment(Comment("newc0_9", "newp0", "Deltarune", "t3_newp0", "v", "zebra quokka", 7, clock.time() - 60, 0, None),
                   0.5, "positive")  # words in no pack yet, so only one index has numbered them
    s.commit()
    posts, comments = RowIndex(path, "posts", "deltarune"), RowIndex(path, "comments", "deltarune")
    for min_post, min_comment in ((None, None), (10, 1), (None, 50)):
        expected = WordCounts.top(WordCounts("posts", scoring_at_least(s.rows("posts", "deltarune", words=True), min_post)),
                                  WordCounts("comments", scoring_at_least(s.rows("comments", "deltarune", words=True), min_comment)),
                                  n=60)
        assert top_words([(posts, min_post), (comments, min_comment)], n=60) == expected
    assert dict((w, n) for w, n, _ in top_words([(posts, None), (comments, None)], n=10_000))["zebra"] == 1
    posts.close()
    comments.close()


def test_an_index_read_without_words_counts_none_and_still_holds_every_row(saved):
    path, s = saved
    posts, comments = RowIndex(path, "posts", "deltarune", words=False), RowIndex(path, "comments", "deltarune", words=False)
    _same_rows(comments, s.rows("comments", "deltarune"))
    for days in (None, (DAY0, DAY0 + 3 * 86400)):
        assert all(not c.any() for c in comments.word_counts(days, 10))
        assert top_words([(posts, None), (comments, 1)], days) == []
    posts.close()
    comments.close()


@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_big_days_are_read_from_their_frames(tmp_path, kind):
    path = tmp_path / "x.db"
    s = Store(path)
    _fill(s, random.Random(8), posts=600, per=1, days=2)  # 300 posts and comments a day: more than a frame
    s.pack_settled()
    assert all(d.startswith(DayPack.MAGIC) for (d,) in s.db.execute("SELECT data FROM packs WHERE kind = ?", (kind,)))
    s.save_comment(Comment("c3_1", "p3", "Deltarune", "t3_p3", "v1", "saved again", 3, DAY0 + 86400 + 3 * 37 + 30, 0,
                           None), 0.1, "positive")  # one in a pack whose row is newer
    s.commit()
    rows = s.rows(kind, "deltarune")
    index = RowIndex(path, kind, "deltarune")
    _same_rows(index, rows)
    for name in list(PACKED[kind]) + (["post_title"] if kind == "comments" else []):
        assert index.column(name, 5, 700) == [r[name] for r in rows[5:700]], name
    assert index.top_words(n=30) == WordCounts.top(WordCounts(kind, s.rows(kind, "deltarune", words=True)), n=30)
    index.close()
    s.close()


def test_a_thread_can_count_words_and_read_a_column_while_the_window_adds_and_draws_rows(saved):
    import threading
    path, s = saved
    index = RowIndex(path, "comments", "deltarune")
    errors, stop = [], threading.Event()

    def worker():
        try:
            while not stop.is_set():
                index.top_words(n=20)
                index.column("body", 0, 100)
                index.column("post_title", 0, 50)
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    thread = threading.Thread(target=worker)
    thread.start()
    rng = random.Random(11)
    for k in range(20):
        since = clock.time()
        clock.sleep(0.002)
        _fill(s, rng, posts=1, per=3, day0=DAY0 + 20 + k, prefix=f"t{k}")
        index.add(s.rows("comments", "deltarune", since=since))
        index.rows(range(0, len(index), 3))
    stop.set()
    thread.join()
    assert not errors
    index.reorder()
    rows = s.rows("comments", "deltarune", words=True)
    _same_rows(index, rows)
    assert index.top_words(n=20) == WordCounts.top(WordCounts("comments", rows), n=20)
    index.close()


def test_rows_added_to_an_empty_index_out_of_time_order_are_sorted_by_reorder(tmp_path):
    path = tmp_path / "x.db"
    s = Store(path)
    index = RowIndex(path, "comments", "deltarune")
    since = clock.time()
    clock.sleep(0.01)
    _fill(s, random.Random(2), posts=2, per=2, day0=DAY0 + 5 * 86400)
    _fill(s, random.Random(3), posts=2, per=2, day0=DAY0, prefix="older")  # saved last, posted first
    index.add(s.rows("comments", "deltarune", since=since))
    assert index.ordered == 0
    day = (DAY0, DAY0 + 86400)
    assert {i.decode() for i in index.ids[index.mask(day)]} == {
        r["id"] for r in s.rows("comments", "deltarune") if day[0] <= r["created_utc"] < day[1]} != set()
    index.reorder()
    _same_rows(index, s.rows("comments", "deltarune"))
    index.close()
    with pytest.raises(ValueError):
        index.column("body")


def test_a_column_has_the_newer_text_of_a_row_saved_again_since_the_index_was_read(saved):
    path, s = saved
    index = RowIndex(path, "comments", "deltarune")
    s.save_comment(Comment("c6_1", "p6", "Deltarune", "t3_p6", "v1", "edited since", 3, DAY0 + 86400 + 6 * 37 + 30, 0,
                           None), 0.1, "positive")
    s.commit()
    at = int(index.where(["c6_1"])[0])
    assert index.column("body", at, at + 1) == ["edited since"]
    index.close()


def _found(rows, kind, typed):
    import re
    words = re.findall(r"\w+", typed.lower())
    out = set()
    for r in rows:
        text = (r["body"] or "") if kind == "comments" else f"{r['title'] or ''} {r['selftext'] or ''}"
        have = set(re.findall(r"\w+", text.lower()))
        if all(w in have for w in words[:-1]) and any(h.startswith(words[-1]) for h in have):
            out.add(r["id"])
    return out


@pytest.mark.skipif(not Search(sqlite3.connect(":memory:")).ready, reason="this SQLite has no contentless deletes (3.43)")
@pytest.mark.parametrize("kind", ["posts", "comments"])
def test_search_finds_every_word_typed_the_last_as_it_is_being_typed(saved, kind):
    path, s = saved
    index = RowIndex(path, kind, "deltarune")
    rows = s.rows(kind, "deltarune")
    for typed in ("boss", "bo", "BOSS fight", "chapter fo", "nerf patch", "zzz"):
        found = {i.decode() for i in index.ids[index.search(typed)]}
        assert found == _found(rows, kind, typed), typed
    assert index.search("  ") is None and index.search("!?") is None
    since = clock.time()
    clock.sleep(0.01)
    s.save_comment(Comment("c6_1", "p6", "Deltarune", "t3_p6", "v1", "a quokka appears", 3, DAY0 + 86400 + 6 * 37 + 30, 0,
                           None), 0.1, "positive")  # saved again with new text
    s.save_post(Post("np1", "Deltarune", "quokka news", "", "a", 1, 0.9, 1, DAY0 + 50, "", ""), 0.0, "neutral")
    s.commit()
    index.add(s.rows(kind, "deltarune", since=since))
    assert {i.decode() for i in index.ids[index.search("quok")]} == ({"c6_1"} if kind == "comments" else {"np1"})
    rows = s.rows(kind, "deltarune")
    for typed in ("boss", "fight", "the"):  # the row saved again is found by its new text only
        assert {i.decode() for i in index.ids[index.search(typed)]} == _found(rows, kind, typed), typed
    index.reorder()
    assert {i.decode() for i in index.ids[index.search("fight")]} == _found(rows, kind, "fight")
    index.close()


def test_rows_a_fetch_adds_are_found_by_id_without_sorting_every_id_again(saved, monkeypatch):
    # Each thread of comments a fetch saved sorted every id of the subreddit again: 65 ms with 190,000 comments.
    path, s = saved
    index = RowIndex(path, "comments", "deltarune")
    assert index.where(["c3_1"])[0] >= 0  # the ids sorted once, here
    sorted_sizes, real_argsort = [], np.argsort
    monkeypatch.setattr(np, "argsort", lambda a, *args, **kw: sorted_sizes.append(len(a)) or real_argsort(a, *args, **kw))
    rng = random.Random(4)
    for n in range(3):
        since = clock.time()
        clock.sleep(0.01)
        _fill(s, rng, posts=3, per=4, day0=DAY0 + 13 + n, prefix=f"more{n}")
        index.add(s.rows("comments", "deltarune", since=since))
    assert sorted_sizes and max(sorted_sizes) <= 12  # only each fetch's own
    at = {i.decode(): p for p, i in enumerate(index.ids)}
    every = [r["id"] for r in s.rows("comments", "deltarune")]
    assert len(at) == len(every) and index.where(every).tolist() == [at[i] for i in every]
    assert index.where(["more9c0_0", "c3_1x"]).tolist() == [-1, -1]
    index.close()


@pytest.mark.parametrize("n", [5, 30, 400])
def test_the_top_words_of_a_new_subreddit_whose_words_are_all_new(tmp_path, n):
    # Downloading a subreddit saved nowhere yet: every word is past the vocab table, in each index numbered on its
    # own, and only those that can make the top n are gone through one by one.
    path = tmp_path / "x.db"
    s = Store(path)
    posts, comments = RowIndex(path, "posts", "deltarune"), RowIndex(path, "comments", "deltarune")
    rng = random.Random(7)
    vocab = [f"w{k}" for k in range(300)]
    chat = lambda: " ".join(rng.choices(vocab, weights=[1 / (k + 1) for k in range(300)], k=rng.randint(1, 12)))  # noqa: E731
    for i in range(60):
        link = f"https://www.reddit.com/r/Deltarune/comments/p{i}/x/"
        s.save_post(Post(f"p{i}", "Deltarune", chat(), chat(), "u", rng.choice(SCORES), 0.9, 8, DAY0 + i * 600, link,
                         link), rng.uniform(-1, 1), rng.choice(LABELS))
        s.save_comments([Comment(f"c{i}_{j}", f"p{i}", "Deltarune", f"t3_p{i}", "v", chat(), rng.choice(SCORES),
                                 DAY0 + i * 600 + j, 0, None) for j in range(8)],
                        [(rng.uniform(-1, 1), rng.choice(LABELS)) for _ in range(8)])
    s.commit()
    posts.add(s.rows("posts", "deltarune", words=True))
    comments.add(s.rows("comments", "deltarune", words=True))
    assert posts._shared == comments._shared == 0
    for min_post, min_comment in ((None, None), (10, 1)):
        expected = WordCounts.top(WordCounts("posts", scoring_at_least(s.rows("posts", "deltarune", words=True), min_post)),
                                  WordCounts("comments", scoring_at_least(s.rows("comments", "deltarune", words=True), min_comment)),
                                  n=n)
        assert top_words([(posts, min_post), (comments, min_comment)], n=n) == expected
    posts.close()
    comments.close()
    s.close()


def test_a_new_word_counts_in_every_index_even_where_it_is_rare(saved):
    # A new word used often in comments and a little in posts: its posts were left out of its count once the
    # comments alone could put it among the top words.
    path, s = saved
    posts, comments = RowIndex(path, "posts", "deltarune"), RowIndex(path, "comments", "deltarune")
    day = int(clock.time()) // 86400 * 86400 - 3600
    link = "https://www.reddit.com/r/Deltarune/comments/zp/x/"
    s.save_post(Post("zp", "Deltarune", "zebraquokka", "", "u", 5, 0.9, 1, day, link, link), 0.5, "positive")
    s.save_comments([Comment(f"zc{j}", "zp", "Deltarune", "t3_zp", "v", "zebraquokka " * 3, 5, day + j, 0, None)
                     for j in range(400)], [(0.5, "positive")] * 400)
    s.commit()
    posts.add(s.rows("posts", "deltarune", words=True))
    comments.add(s.rows("comments", "deltarune", words=True))
    expected = WordCounts.top(WordCounts("posts", s.rows("posts", "deltarune", words=True)),
                              WordCounts("comments", s.rows("comments", "deltarune", words=True)), n=5)
    got = top_words([(posts, None), (comments, None)], n=5)
    assert got == expected and dict((w, k) for w, k, _ in got)["zebraquokka"] == 401
    posts.close()
    comments.close()
