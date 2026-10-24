"""The word cloud: which words count, how they're tallied and placed, and the Words page on real saved data."""
import math
from collections import Counter
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt, QVariantAnimation  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment import sentiment  # noqa: E402

from subsentiment.analysis import WordCounts, words_in  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import motion  # noqa: E402
from subsentiment.gui.rows import Saved  # noqa: E402
from subsentiment.gui.words import KINDS, WordCloud, WordMeanings, WordTally  # noqa: E402
from subsentiment.reddit import Comment, Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402


def comment(i, body, label="neutral"):
    return {"id": f"c{i}", "body": body, "label": label}


def test_only_words_that_say_something_count():
    text = ("Kris's sword is GREAT, it’s the best!! See https://example.com/x and www.example.org &amp; "
            "2024—Susie’s pokémon [link](/r/x) 'quoted' 42 ok")
    assert words_in(text) == {"kris", "sword", "great", "best", "susie", "pokémon", "link", "quoted"}


def test_a_word_counts_once_per_item_and_leans_with_their_labels():
    counts = WordCounts("comments", [comment(1, "great game, great music", "positive"),
                                     comment(2, "bad game", "negative"), comment(3, "game night")])
    assert WordCounts.top(counts) == [("game", 3, 0.0), ("bad", 1, -1.0), ("great", 1, 1.0), ("music", 1, 1.0),
                                      ("night", 1, 0.0)]  # ties alphabetically, so a cloud doesn't reshuffle


def test_a_row_saved_again_replaces_its_words():
    counts = WordCounts("comments", [comment(1, "bad game", "negative")])
    counts.add([comment(1, "good game", "positive")])  # rescored and edited by a later fetch
    assert WordCounts.top(counts) == [("game", 1, 1.0), ("good", 1, 1.0)]


def test_posts_count_their_title_and_text_with_comments_together():
    posts = WordCounts("posts", [{"id": "p1", "title": "Patch notes", "selftext": None, "label": "positive"}])
    comments = WordCounts("comments", [comment(1, "the patch broke saves", "negative")])
    assert dict((w, (n, lean)) for w, n, lean in WordCounts.top(posts, comments))["patch"] == (2, 0.0)


def test_the_cloud_fits_its_words_without_overlap_biggest_first():
    QApplication.instance() or QApplication([])
    cloud = WordCloud()
    cloud.resize(700, 300)
    cloud.set_words([(f"word{i}", 1000 // (i + 1), (i % 5 - 2) / 2) for i in range(60)])  # a few common, many rare
    placed = cloud.placed()
    assert len(placed) > 50
    boxes = [b for b, *_ in placed]
    assert all(cloud.rect().toRectF().contains(b) for b in boxes)
    assert not any(a.intersects(b) for i, a in enumerate(boxes) for b in boxes[i + 1:])
    sizes = [px for *_, px in placed]
    assert sizes == sorted(sizes, reverse=True) and sizes[0] > 2 * sizes[-1]
    cloud.set_words([])
    assert cloud.placed() == []


def test_a_change_eases_in_from_the_words_on_screen_and_reduce_motion_lands_at_once(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(motion, "_os_allows", True)
    words = [(f"word{i}", 1000 // (i + 1), 0.5) for i in range(30)]
    for on in (True, False):
        monkeypatch.setattr(motion, "_wanted", on)
        cloud = WordCloud()
        cloud.resize(700, 300)
        cloud.show()
        app.processEvents()
        cloud.set_words(words)
        was = {word: box.center() for box, word, *_ in cloud.placed()}
        cloud._anim.setCurrentTime(cloud.MOVE_MS)  # the words have popped in
        cloud.repaint()  # on screen: the next change eases in from here
        assert cloud._painted
        # Counts change, the least used word goes, a new one comes and the third most used turns negative.
        cloud.set_words([(w, n + 5, -0.8 if i == 2 else lean) for i, (w, n, lean) in enumerate(words[:-1])]
                        + [("newcomer", 36, 0.0)])
        now = {word: box.center() for box, word, *_ in cloud.placed()}
        assert "newcomer" in now and "word29" not in now
        # Most stayed put, or took just the small step towards the middle that closes up gaps.
        mid = cloud.rect().toRectF().center()
        assert sum(math.dist(was[k].toTuple(), now[k].toTuple()) <= cloud.PULL * math.dist(was[k].toTuple(), mid.toTuple()) + 1
                   for k in now if k in was) > 20
        drawn = {w.word: w for w in cloud._shown.values()}
        assert cloud._anim.state() == (QVariantAnimation.State.Running if on else QVariantAnimation.State.Stopped)
        if on:  # the first frame: as before, the new word not there yet and the one going still fully there
            assert drawn["newcomer"].alpha == 0 and drawn["word29"].alpha == 1 and drawn["word29"].shown == 0
            assert drawn["word2"].was == 0.5 and drawn["word2"].lean == -0.8  # its colour fades across
            cloud._anim.setCurrentTime(cloud.MOVE_MS // 2)
            assert 0 < drawn["newcomer"].alpha < 1 and 0 < drawn["word29"].alpha < 1
            cloud.repaint()  # a frame mid-way draws the pictures of the words
            cloud._anim.setCurrentTime(cloud.MOVE_MS)
            app.processEvents()
        drawn = {w.word: w for w in cloud._shown.values()}
        assert set(drawn) == set(now)  # the word that went is gone
        assert all(abs(w.x - now[k].x()) < 1e-6 and abs(w.y - now[k].y()) < 1e-6 and w.alpha == 1
                   and w.size == w.px for k, w in drawn.items())
        cloud.close()


def test_a_narrow_cloud_shrinks_its_longest_most_used_words_rather_than_leaving_them_out():
    QApplication.instance() or QApplication([])
    cloud = WordCloud()
    cloud.resize(150, 560)  # the Words page beside its meanings card, on a small window
    cloud.set_words([("festival", 2, 0.5), ("harbour", 2, 0.0), ("grinding", 2, -0.5)]
                    + [(w, 1, 0.0) for w in ("tonight", "parking", "awful", "library")])
    placed = cloud.placed()
    assert {word for _, word, *_ in placed} == {"festival", "harbour", "grinding", "tonight", "parking", "awful",
                                                 "library"}
    assert all(cloud.rect().toRectF().contains(b) for b, *_ in placed)
    # Sized for the width, so the most used aren't each shrunk by a different amount: words used as often are
    # the same size, and the more used never smaller.
    sizes = {word: px for _, word, _, _, px in placed}
    assert sizes["festival"] == sizes["harbour"] == sizes["grinding"] > sizes["tonight"] == sizes["library"]


def test_a_little_less_room_keeps_the_words_where_they_are_and_a_lot_lays_them_out_afresh():
    # A fetch's strip appearing under the header makes the page a little shorter: that used to lay the whole
    # cloud out again, a jump just as a fetch began.
    QApplication.instance() or QApplication([])
    cloud = WordCloud()
    cloud.resize(700, 300)
    cloud.set_words([(f"w{i}", 100 - 3 * i, 0.0) for i in range(25)])
    was = {word: box.center().toTuple() for box, word, *_ in cloud.placed()}
    cloud.resize(700, 280)
    now = {word: box.center().toTuple() for box, word, *_ in cloud.placed()}
    moved = sorted(math.dist(was[k], now[k]) for k in now if k in was)
    assert len(moved) >= 20 and moved[len(moved) // 2] < 15  # each moved with the edge, and a small step at most
    cloud.resize(1400, 600)
    big = {word: box.center().toTuple() for box, word, *_ in cloud.placed()}
    assert all(abs(big[k][0] - 700) < 700 for k in big) and len(big) == 25  # filled afresh, every word fits


def saved_comments(path, sub, bodies) -> dict:
    """A subreddit with these comments saved (no posts), as the window reads it: {kind: rows.Saved}."""
    s = Store(path)
    for i, body in enumerate(bodies):
        s.save_comment(Comment(f"{sub}{i}", "p", sub, "t3_p", "b", body, 1, 1_700_000_000.0 + i, 0, "/r/x/"), 0.0,
                       "neutral")
    s.commit()
    s.close()
    return {k: Saved(str(path), k, sub) for k in KINDS}


def test_a_big_recount_runs_on_a_thread_and_takes_rows_added_meanwhile(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(WordTally, "THREAD_FROM", 10)
    saved = saved_comments(tmp_path / "w.db", "big", ["river market"] * 2000)
    tally = WordTally()
    done = []
    tally.changed.connect(lambda: done.append(True))
    tally.count(saved, None, (None, None))
    assert not tally.ready  # counting on its thread; the window carries on
    saved["comments"].add([dict(comment(5000, "river bridge"), score=1, sentiment=0.0, created_utc=1_700_000_000.0)])
    tally.count(saved, None, (None, None))  # a fetch saved one meanwhile, and the overview refreshed
    end = time.monotonic() + 10
    while not (tally.ready and not tally._slices.running()):
        assert time.monotonic() < end
        app.processEvents()
        time.sleep(0.01)
    assert dict((w, n) for w, n, _ in tally.top(["comments"])) == {"river": 2001, "market": 2000, "bridge": 1}
    assert done


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    s = Store(tmp_path / "w.db")
    day = 1_700_000_000.0
    for i, (title, label) in enumerate([("Harbour festival tonight", "positive"), ("Harbour parking awful", "negative"),
                                        ("Library reopened", "positive")]):
        s.save_post(Post(f"p{i}", "town", title, "", "a", 1, 0.9, 0, day - i * 20 * 86400, "https://r", ""), 0, label)
    s.save_comment(Comment("c1", "p0", "town", "t3_p0", "b", "the festival lights were lovely", 1, day + 60, 0,
                           "/r/town/comments/p0/c1/"), 0.6, "positive")
    s.commit()
    s.close()
    w = mw.MainWindow(str(tmp_path / "w.db"))
    yield w
    w.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_the_words_page_shows_the_saved_words_and_follows_scope_and_span(window):
    w = window
    w.show()
    w.show_page("words")
    QApplication.processEvents()
    page = w.words_page.body

    def shown():
        return {word: n for _, word, n, *_ in page.placed()}
    assert shown() == {"harbour": 2, "festival": 2, "tonight": 1, "parking": 1, "awful": 1, "library": 1,
                       "reopened": 1, "lights": 1, "lovely": 1}
    assert {word for _, word, *_ in w.overview.words.placed()} == set(shown())
    w.scope.setCurrentText("Comments only")
    assert shown() == {"festival": 1, "lights": 1, "lovely": 1}
    w.scope.setCurrentText("Posts and comments")
    w.overview.timeline.presets["1w"].click()  # the newest week: the first post and its comment
    w._apply_span()  # what the tables and the cloud do once the span settles
    assert shown() == {"harbour": 1, "festival": 2, "tonight": 1, "lights": 1, "lovely": 1}


def test_a_small_count_lets_a_big_one_still_running_go(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(WordTally, "THREAD_FROM", 10)
    big = saved_comments(tmp_path / "w.db", "big", ["river market"] * 2000)
    small = saved_comments(tmp_path / "w.db", "small", ["quiet lane"])
    tally = WordTally()
    tally.count(big, None, (None, None))
    tally.count(small, None, (None, None))  # then a small one is chosen: counted at once
    assert tally.ready and [w for w, *_ in tally.top(["comments"])] == ["lane", "quiet"]
    end = time.monotonic() + 3
    while time.monotonic() < end:  # the big count ends and is let go
        app.processEvents()
        time.sleep(0.01)
    assert [w for w, *_ in tally.top(["comments"])] == ["lane", "quiet"]


def test_setting_a_word_rescores_that_subreddit_only_and_every_page_follows(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    s = Store(tmp_path / "w.db")
    day = 1_700_000_000.0  # long settled: the text is packed (store.PACKED), and read back through the packs
    for sub in ("town", "city"):
        for i, title in enumerate(["Grinding the boss all night", "Library reopened today", "Grinding again"]):
            s.save_post(Post(f"{sub}{i}", sub, title, "", "a", 1, 0.9, 0, day - i * 86400, "https://r", ""),
                        *sentiment.score(title))
        s.save_comment(Comment(f"{sub}c", f"{sub}0", sub, "t3_x", "b", "more grinding tonight", 1, day + 60, 0, "/r/x/"),
                       *sentiment.score("more grinding tonight"))
    s.commit()
    assert s.pack_settled() > 0
    before = {r["id"]: r["label"] for r in s.posts("city") + s.comments("city")}
    assert "positive" not in before.values()
    s.close()
    w = mw.MainWindow(str(tmp_path / "w.db"))
    try:
        w.show()
        w._refresh_datasets("town")
        w.show_page("words")
        QApplication.processEvents()
        assert w.current == "town"
        positive = w._counted.counts["positive"]
        meanings = w.words_page.meanings
        cloud = w.words_page.body
        box = next(b for b, word, *_ in cloud.placed() if word == "grinding")
        QTest.mouseClick(cloud, Qt.MouseButton.LeftButton, pos=box.center().toPoint())  # picks it, unset
        assert meanings.rows["grinding"].reads.text() == "Not set" and w.store.words("town") == {}
        row = meanings.rows["grinding"]
        QTest.mouseClick(row.slider, Qt.MouseButton.LeftButton, pos=QPoint(row.slider.width() - 4, row.slider.height() // 2))
        assert w.store.words("town") == {"grinding": 1.0} and row.reads.text() == "Very positive"
        assert row.result.text().startswith("Scoring saved items\u2026")  # the owner: a way to see it's working
        end = time.monotonic() + 10
        while w.rescore.running():
            assert time.monotonic() < end
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        saved = {r["id"]: r["label"] for r in w.store.posts("town") + w.store.comments("town")}
        assert saved == {"town0": "positive", "town1": before["city1"], "town2": "positive", "townc": "positive"}
        assert {r["id"]: r["label"] for r in w.store.posts("city") + w.store.comments("city")} == before
        assert w._counted.counts["positive"] == positive + 3  # the overview and timeline
        assert {r["id"]: r["label"] for r in w.posts_tab.model.rows}["town2"] == "positive"  # the tables
        assert {r["id"]: r["label"] for r in w.posts}["town0"] == "positive"  # and what export writes
        assert dict((word, lean) for word, _, lean in w.word_tally.top(KINDS))["grinding"] == 1.0  # the cloud
        assert meanings.status.isHidden()  # only says something when scoring failed
        moved = {(a, "positive"): sum(1 for i in ("town0", "town2", "townc") if before[i.replace("town", "city")] == a)
                 for a in ("negative", "neutral")}
        assert row.result.text() == "\u2713 3 of 3 saved items changed label"
        assert row.result.toolTip() == "\n".join(
            f"{n} {a} \u2192 {b}" for (a, b), n in sorted(moved.items(), key=lambda m: -m[1]) if n)
        w._refresh_datasets("city")
        assert meanings.subreddit == "city" and meanings.rows == {}
        w._refresh_datasets("town")  # back from memory, rescored
        assert set(meanings.rows) == {"grinding"} and w._counted.counts["positive"] == positive + 3
        QTest.mouseClick(meanings.rows["grinding"].remove, Qt.MouseButton.LeftButton)
        end = time.monotonic() + 10
        while w.rescore.running():
            assert time.monotonic() < end
            app.processEvents()
            time.sleep(0.01)
        assert w.store.words("town") == {} and w._counted.counts["positive"] == positive
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_word_set_while_its_subreddit_is_fetched_is_rescored_again_after(window):
    w = window
    w.show()
    app = QApplication.instance()
    started = []
    w.rescore.start = lambda sub, words, rows=None: started.append((sub, set(words), rows is None))
    w.request, w.fetch_job.qthread = mw.collect.FetchRequest("town"), object()  # as if town were being fetched
    w.words_page.meanings.pick("harbour")
    w.words_page.meanings._save(w.words_page.meanings.rows["harbour"])
    assert started == [("town", {"harbour"}, True)]  # read from disk: the window doesn't have every row yet
    w.fetch_job.qthread = None
    w._rescore_after_fetch()  # as the fetch ends: what it saved meanwhile was scored the old way
    assert started[-1] == ("town", {"harbour"}, True)
    w._rescore_after_fetch()
    assert len(started) == 2
    app.processEvents()


def test_moving_the_span_keeps_the_words_both_spans_share_where_they_are(window, monkeypatch):
    """The owner: the cloud should move fluidly "not just when you scrape but when you move around the timeline"."""
    w, app = window, QApplication.instance()
    w.show()
    w.show_page("words")
    app.processEvents()
    page = w.words_page.body
    was = {word: box.center().toTuple() for box, word, *_ in page.placed()}
    page.repaint()
    w.overview.timeline.presets["1w"].click()  # the newest week: the first post and its comment
    w._apply_span()  # what the tables and the cloud do once the span settles
    now = {word: box.center().toTuple() for box, word, *_ in page.placed()}
    mid = page.rect().toRectF().center().toTuple()
    assert set(now) == {"harbour", "festival", "tonight", "lights", "lovely"}
    # Each stayed put, or took at most a small step towards the middle (closing up the gaps words left).
    assert all(math.dist(was[k], now[k]) <= 0.15 * math.dist(was[k], mid) + 1 for k in now), (was, now)

    # A recount too big to do at once, and the clouds shown again meanwhile (as a fetch's refresh does): the
    # words stay up, faded, until it's done, rather than the cloud emptying and filling again from nothing.
    monkeypatch.setattr(WordTally, "THREAD_FROM", 1)
    w.overview.timeline.presets["All"].click()
    w._apply_span()
    w._render_words()
    assert not w.word_tally.ready and page.busy
    assert {word for _, word, *_ in page.placed()} == set(now) and page._dimming.target == page.BUSY_DIM
    end = time.monotonic() + 10
    while not w.word_tally.ready:
        assert time.monotonic() < end
        app.processEvents()
    assert not page.busy and page._dimming.target == 1.0 and "library" in {word for _, word, *_ in page.placed()}


def test_the_result_counts_only_what_the_setting_changes_and_old_scores_apart(tmp_path):
    """The owner set "dungeon" to neutral and read "130 neutral \u2192 positive, 50 negative \u2192 positive": most of that
    was items scored by an older version getting today's score, not the setting."""
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    s = Store(tmp_path / "w.db")
    day = 1_700_000_000.0
    texts = ["Grinding the boss all night", "Grinding again", "The grinding is awful and broken, I hate it",
             "Library reopened today"]
    for i, title in enumerate(texts):
        score, label = sentiment.score(title)
        if i == 2:
            label = "positive"  # as an older version had it
        s.save_post(Post(f"p{i}", "town", title, "", "a", 1, 0.9, 0, day - i * 86400, "https://r", ""), score, label)
    s.commit()
    s.close()
    set_to = sentiment.Lexicon({"grinding": 1.0})
    usual = {t: sentiment.score(t)[1] for t in texts[:3]}
    now = {t: set_to.score(t)[1] for t in texts[:3]}
    assert usual[texts[2]] == now[texts[2]] == "negative"  # what the setting doesn't change, but was out of date
    changes = Counter((usual[t], now[t]) for t in texts[:2] if usual[t] != now[t])
    assert changes
    w = mw.MainWindow(str(tmp_path / "w.db"))
    try:
        w.show()
        w._refresh_datasets("town")
        meanings = w.words_page.meanings
        meanings.pick("grinding")
        meanings.rows["grinding"].slider.setValue(100)
        meanings._save(meanings.rows["grinding"])
        end = time.monotonic() + 10
        while w.rescore.running():
            assert time.monotonic() < end
            app.processEvents()
            time.sleep(0.01)
        n = sum(changes.values())
        result = meanings.rows["grinding"].result
        assert result.text() == f"\u2713 {n} of 3 saved items changed label"
        assert result.toolTip() == "\n".join([f"{k} {a} \u2192 {b}" for (a, b), k in changes.most_common()]
                                             + ["Also updated 1 scored by an older version"])
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_word_set_where_the_window_doesnt_have_every_row_rescores_from_the_saved_ones(tmp_path):
    """With a score filter on (or another subreddit shown), the rescore reads the subreddit itself. It failed at
    once: its read was asked for one minimum score, where it takes one for posts and one for comments."""
    QApplication.instance() or QApplication([])
    from subsentiment.gui.words import Rescore
    s = Store(tmp_path / "w.db")
    s.save_post(Post("p1", "town", "Grinding the boss all night", "", "a", 1, 0.9, 0, 1_700_000_000.0, "https://r", ""),
                *sentiment.score("Grinding the boss all night"))
    s.commit()
    s.save_word("town", "grinding", 1.0)
    rescore = Rescore(s, str(tmp_path / "w.db"))
    failed, done = [], []
    rescore.failed.connect(lambda sub, message: failed.append(message))
    rescore.finished.connect(done.append)
    rescore.start("town", {"grinding"})  # no rows given: read on a thread
    end = time.monotonic() + 10
    while not done and not failed:
        assert time.monotonic() < end
        QApplication.processEvents()
        time.sleep(0.01)
    assert failed == [] and done == ["town"]
    assert s.posts("town")[0]["label"] == "positive"
    rescore.stop()
    s.close()


@pytest.mark.parametrize("processes, again", [(True, False), (False, False), (True, True)],
                         ids=["in other processes", "where they can't run", "set again while it runs"])
def test_a_big_rescore_is_scored_a_chunk_at_a_time_in_other_processes(tmp_path, monkeypatch, processes, again):
    """The owner's r/classicwow had 31,497 items using "dungeon", which took minutes to score on the window's own
    thread. Chunks go to worker processes instead; where those can't start, every item is still scored here.
    A word set while a rescore runs starts it again, with new processes: what the old ones were on is dropped."""
    from subsentiment.gui import worker, words

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    monkeypatch.setattr(words.Rescore, "CHUNK", 4)
    if not processes:
        def refuse(*a, **k):
            raise OSError("no processes here")
        monkeypatch.setattr(worker, "ProcessPoolExecutor", refuse)
    s = Store(tmp_path / "w.db")
    day = 1_700_000_000.0
    texts = [f"Grinding {what} today" for what in ("is fun", "again", "is awful", "for hours", "the boss", "sucks",
                                                    "with friends", "is broken", "forever", "is great", "alone")]
    texts += ["Library reopened today", "The weather is lovely"]
    for i, title in enumerate(texts):
        s.save_post(Post(f"p{i}", "town", title, "", "a", 1, 0.9, 0, day - i * 3600, "https://r", ""), *sentiment.score(title))
    s.commit()
    s.close()
    w = mw.MainWindow(str(tmp_path / "w.db"))
    pooled = []
    w.rescore.pool.scored.connect(lambda tag, results: pooled.append(len(results)))
    try:
        w.show()
        w._refresh_datasets("town")
        meanings = w.words_page.meanings
        meanings.pick("grinding")
        meanings.rows["grinding"].set(-1.0)  # as if dragged there: no wait for a slider moved with the keys
        meanings._save(meanings.rows["grinding"])
        if again:
            until_out = time.monotonic() + 30
            while not w.rescore._out:  # some chunks are with the processes
                assert time.monotonic() < until_out
                app.processEvents()
            first = w.rescore.pool._pool
            meanings.pick("library")
            meanings.rows["library"].set(0.0)
            meanings._save(meanings.rows["library"])
            assert first._shutdown_thread and w.rescore.pool._pool is not first
        end = time.monotonic() + 60
        while w.rescore.running():
            assert time.monotonic() < end
            app.processEvents()
            time.sleep(0.01)
        assert w.rescore.pool.broken == (not processes)
        set_to = sentiment.Lexicon({"grinding": -1.0, **({"library": 0.0} if again else {})})
        assert {r["id"]: r["label"] for r in w.store.posts("town")} == {
            f"p{i}": set_to.score(t)[1] for i, t in enumerate(texts)}
        if not again:
            assert sorted(pooled) == ([3, 4, 4] if processes else [])  # back in any order
        assert (w.rescore.pool._pool is not None) == processes  # kept for a word set next
        assert w.rescore.pool._idle.isActive() == processes  # until a minute after
        w.rescore.pool._idle.timeout.emit()
        assert w.rescore.pool._pool is None
        changes = sum(1 for t in texts[:11 + again] if set_to.score(t)[1] != sentiment.score(t)[1])
        assert meanings.rows["grinding"].result.text() == f"\u2713 {changes} of {11 + again} saved items changed label"
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_rescore_saves_on_a_thread_and_the_cloud_follows_once_it_is_done(tmp_path, monkeypatch):
    """Setting a word on a big subreddit janked the window for 40 s: every quarter second's scores were saved and
    committed on its thread (up to half a second with SQLite's checkpoint), each packed item's text was read and
    split again for the word counts, and the clouds and tint eased to every batch (the cloud drawn 900 times). Now
    a thread saves them, the words come split with the scores, and the timeline follows each batch while the
    clouds are counted once, at the end."""
    import threading

    from subsentiment.gui import worker, words
    from subsentiment.index import RowIndex

    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    monkeypatch.setattr(words.Rescore, "CHUNK", 4)
    monkeypatch.setattr(words.Rescore, "BATCH", 0.0)  # a batch an item: as many as a big subreddit has
    monkeypatch.setattr(worker, "ProcessPoolExecutor", lambda *a, **k: (_ for _ in ()).throw(OSError("none here")))
    s = Store(tmp_path / "w.db")
    day = 1_700_000_000.0  # long settled: packed
    texts = [f"Grinding {what} today" for what in ("is fun", "again", "is awful", "for hours", "the boss", "sucks",
                                                    "with friends", "is broken", "forever", "is great")]
    for i, title in enumerate(texts + ["Library reopened today"]):
        s.save_post(Post(f"p{i}", "town", title, "", "a", 1, 0.9, 0, day - i * 3600, "https://r", ""), *sentiment.score(title))
    s.commit()
    assert s.pack_settled() > 0
    s.close()
    saving, split_here = [], []
    rescore = Store.rescore
    monkeypatch.setattr(Store, "rescore", lambda self, *a: (saving.append(threading.current_thread()), rescore(self, *a)))
    words_of = RowIndex._words_of
    monkeypatch.setattr(RowIndex, "_words_of", lambda self, t: (split_here.append(t), words_of(self, t))[1])
    w = mw.MainWindow(str(tmp_path / "w.db"))
    try:
        w.show()
        w._refresh_datasets("town")
        w.show_page("words")
        app.processEvents()
        batches, counts = [], []
        w.rescore.rescored.connect(lambda sub, kind, rows: batches.append(len(rows)))
        tally = w.word_tally.count
        monkeypatch.setattr(w.word_tally, "count", lambda *a: (counts.append(len(batches)), tally(*a)))
        positive = w._counted.counts["positive"]
        meanings = w.words_page.meanings
        meanings.pick("grinding")
        meanings.rows["grinding"].set(1.0)
        meanings._save(meanings.rows["grinding"])
        end = time.monotonic() + 30
        while w.rescore.running():
            assert time.monotonic() < end
            app.processEvents()
            time.sleep(0.005)
        app.processEvents()
        assert len(batches) >= 3 and saving and threading.main_thread() not in saving
        assert split_here == []  # the words came with the scores
        assert counts == [len(batches)]  # once, after the last batch
        set_to = sentiment.Lexicon({"grinding": 1.0})
        assert {r["id"]: r["label"] for r in w.store.posts("town")} == {
            f"p{i}": set_to.score(t)[1] for i, t in enumerate(texts + ["Library reopened today"])}
        assert w._counted.counts["positive"] == positive + sum(
            set_to.score(t)[1] == "positive" and sentiment.score(t)[1] != "positive" for t in texts) - sum(
            set_to.score(t)[1] != "positive" and sentiment.score(t)[1] == "positive" for t in texts)
        assert dict((word, lean) for word, _, lean in w.word_tally.top(KINDS))["grinding"] == pytest.approx(
            sum((set_to.score(t)[1] == "positive") - (set_to.score(t)[1] == "negative") for t in texts) / len(texts))
        assert {r["id"]: r["label"] for r in w.posts_tab.model.rows} == {
            r["id"]: r["label"] for r in w.store.posts("town")}
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_setting_that_changes_no_label_says_why():
    """The owner set "zenless" to Neutral and read "None of 61 saved items changed label", which looked broken:
    neither scorer knows the word, so it read as neutral already."""
    app = QApplication.instance() or QApplication([])
    meanings = WordMeanings()
    meanings.set_subreddit("ZZZ", {"zenless": 0.0, "hoyo": 0.0, "wow": 0.0, "great": 0.3})
    meanings.show_result("zzz", {"zenless"}, 61, Counter())
    meanings.show_result("zzz", {"wow"}, 1, Counter())
    meanings.show_result("zzz", {"great"}, 4, Counter({("neutral", "positive"): 2}))
    app.processEvents()
    assert meanings.rows["zenless"].result.text() == (
        "✓ No labels changed. 61 saved items use “zenless”, but the scorer doesn't know the word, so "
        "it already read as neutral.")
    assert meanings.rows["wow"].result.text() == (
        "✓ No labels changed. 1 saved item uses “wow”, but the setting wasn't enough to move any of "
        "them to another label.")
    assert meanings.rows["great"].result.text() == "✓ 2 of 4 saved items changed label"
