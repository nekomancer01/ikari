import random
import sqlite3
import time as clock

import numpy as np
import pytest

from subsentiment.analysis import Analysis, WordCounts
from subsentiment.index import STEPS, RowIndex
from subsentiment.reddit import Comment, Post
from subsentiment.store import PACKED, DayPack, Store, scoring_at_least

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
    _fill(s, rng, posts=4, per=3, day0=DAY0 + 11, prefix="fresh")  # new rows on old days: the index re-sorts
    s.save_comment(Comment("c3_0", "p3", "Deltarune", "t3_p3", "v0", "edited text", 77, DAY0 + 3 * 86400 + 113, 0,
                           "https://www.reddit.com/r/Deltarune/comments/p3/x/c3_0/"), -0.2, "negative")
    s.commit()
    index.add(s.rows("comments", "deltarune", since=since))
    s.rescore("comments", [("c9_3", 0.7, "positive")])
    s.commit()
    index.rescore([("c9_3", 0.7, "positive")])
    assert len(index) == before + 12
    rows = s.rows("comments", "deltarune", words=True)
    _same_rows(index, rows)
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
