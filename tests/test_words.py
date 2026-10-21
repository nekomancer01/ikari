"""The word cloud: which words count, how they're tallied and placed, and the Words page on real saved data."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment import sentiment  # noqa: E402

from subsentiment.analysis import WordCounts, words_in  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui.words import KINDS, WordCloud, WordTally  # noqa: E402
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


def test_a_big_recount_runs_on_a_thread_and_keeps_rows_added_meanwhile(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(WordTally, "THREAD_FROM", 10)
    tally = WordTally()
    done = []
    tally.changed.connect(lambda: done.append(True))
    tally.count([], [comment(i, "river market") for i in range(2000)])
    assert not tally.ready  # counting on its thread; the window carries on
    tally.add("comments", [comment(5000, "river bridge")])  # a fetch saved one meanwhile
    end = time.monotonic() + 10
    while not done:
        assert time.monotonic() < end
        app.processEvents()
        time.sleep(0.01)
    assert tally.ready and dict((w, n) for w, n, _ in tally.top(["comments"])) == {
        "river": 2001, "market": 2000, "bridge": 1}


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


def test_a_small_recount_drops_rows_added_for_a_big_one_still_running(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(WordTally, "THREAD_FROM", 10)
    tally = WordTally()
    tally.count([], [comment(i, "river market") for i in range(2000)])
    tally.add("comments", [comment(5000, "river bridge")])  # saved for the big subreddit meanwhile
    tally.count([], [comment(1, "quiet lane")])  # then a small one is chosen: counted at once
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
        assert meanings.status.text() == "Saved items scored again"
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
