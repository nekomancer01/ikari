"""A big saved subreddit (the owner's has 650,000 comments) opens without holding up the window or filling memory. Work
and memory are counted, not timed, so these hold on a slow runner too; tools/perf.py --db measures the frame times
on a real database."""
import os
import sqlite3
import sys
import threading
import time
import tracemalloc
from collections import Counter

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment import analysis as analysis_module  # noqa: E402
from subsentiment.analysis import Analysis, WordCounts  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import tables  # noqa: E402
from subsentiment.gui.tables import COMMENT_COLUMNS as TABLE_COMMENTS, TablePage  # noqa: E402
from subsentiment.gui.rows import RowSource, Saved  # noqa: E402
from subsentiment.gui.words import WordTally  # noqa: E402
from subsentiment.gui.worker import LoadWorker  # noqa: E402
from subsentiment import store as store_module  # noqa: E402
from subsentiment.store import Search, Store, scoring_at_least  # noqa: E402

from test_big_data import COMMENTS, POSTS, count_calls, fill  # noqa: E402
from test_responsive import until  # noqa: E402


def settings(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)


def test_the_app_shows_its_window_before_reading_a_big_subreddit(tmp_path, monkeypatch):
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    QSettings().setValue("ui/last_dataset", "Big")
    reads = [count_calls(monkeypatch, Store, name) for name in ("rows", "posts", "comments")]
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)  # as the app opens it
    w = mw.MainWindow(db)
    try:
        # Reading the owner's 650,000 comments took 17 s before the window first showed.
        assert reads == [[], [], []] and w.subheading.text() == "Opening r/Big…"
        w.show()
        until(lambda: len(w._saved["comments"]) == COMMENTS and not w._span_slices.running(), 30)
        assert w.current == "Big" and w.comments_tab.model.rowCount() == COMMENTS
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_big_subreddit_takes_a_tenth_of_the_memory_it_did(tmp_path):
    db = str(tmp_path / "big.db")
    fill(db)
    Store(db).pack_settled()  # as the owner's are: days settle and are packed two days after they're fetched
    got = []
    worker = LoadWorker(db, "Big")
    worker.read.connect(got.append)
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        worker.run()  # as the window reads it, on this thread instead of its own
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    (_, saved), = got
    assert len(saved["comments"]) == COMMENTS and saved["comments"].analysis(None).count == COMMENTS
    # A dict per row took about 1,040 bytes a row here; a tuple of its values, with what rows share kept once,
    # about 530 and its words as much again. A RowIndex keeps each row's numbers in arrays, and each day's word
    # counts, its text staying on disk until a table draws the row.
    assert held / (POSTS + COMMENTS) < 100, held / (POSTS + COMMENTS)


class Counted(dict):
    """A row that counts how often its id is read."""
    reads = 0

    def __getitem__(self, key):
        if key == "id":
            Counted.reads += 1
        return super().__getitem__(key)


def test_the_words_of_a_big_span_are_counted_on_a_thread_from_its_days_counts(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db = str(tmp_path / "big.db")
    fill(db)
    saved = {k: Saved(db, k, "Big") for k in ("posts", "comments")}
    tally = WordTally()
    tally.count(saved, None, (None, None))
    until(lambda: tally.ready and not tally._slices.running(), 30)
    split, real = [], analysis_module.words_in
    monkeypatch.setattr(analysis_module, "words_in", lambda text: split.append(1) or real(text))
    rows = Store(db).comments("Big")
    span = (int(min(r["created_utc"] for r in rows) // 86400 + 3) * 86400, int(time.time() // 86400 - 3) * 86400)
    tally.count(saved, span, (None, None))  # the span moved
    # Main went through every row of the span to see which were new (most of a second with the owner's 650,000
    # comments); it adds up the counts of the span's days on a thread now, the words shown meanwhile.
    assert not tally.ready and tally._slices.running()
    until(lambda: tally.ready and not tally._slices.running(), 30)
    assert split == []
    inside = [r for r in rows if span[0] <= r["created_utc"] < span[1]]
    posts = [r for r in Store(db).posts("Big") if span[0] <= r["created_utc"] < span[1]]
    assert tally.top(["posts", "comments"], 60) == WordCounts.top(WordCounts("posts", posts),
                                                                  WordCounts("comments", inside), n=60)


def test_a_comment_fetched_while_the_words_are_counted_comes_into_the_cloud(tmp_path):
    QApplication.instance() or QApplication([])
    db = str(tmp_path / "big.db")
    fill(db)
    saved = {k: Saved(db, k, "Big") for k in ("posts", "comments")}
    tally = WordTally()
    tally.count(saved, None, (None, None))
    fetched = {"id": "new", "body": "zebraword", "label": "positive", "sentiment": 0.5, "score": 1,
               "created_utc": time.time() - 3600}
    saved["comments"].add([fetched])  # saved by a running fetch before the count is in
    tally.count(saved, None, (None, None))
    until(lambda: tally.ready and not tally._slices.running(), 30)
    assert ("zebraword", 1, 1.0) in tally.top(["comments"], 10_000)


def test_the_words_of_posts_and_comments_together_are_found_without_adding_up_every_count(monkeypatch):
    QApplication.instance() or QApplication([])
    posts = [{"id": f"p{i}", "title": f"patch {i % 7} notes", "selftext": "", "label": "positive"} for i in range(300)]
    comments = [{"id": f"c{i}", "body": f"word{i % 3000} river w{i % 11}", "label": "negative"} for i in range(20_000)]
    parts = WordCounts("posts", posts), WordCounts("comments", comments)
    added = []
    real = Counter.__add__
    monkeypatch.setattr(Counter, "__add__", lambda a, b: added.append(1) or real(a, b))
    top = WordCounts.top(*parts, n=150)
    # Adding a big subreddit's whole counts up took ~60 ms each time the cloud was drawn.
    assert added == []
    total = {w: parts[0].counts["all"][w] + parts[1].counts["all"][w]
             for w in set(parts[0].counts["all"]) | set(parts[1].counts["all"])}
    assert [(w, n) for w, n, _ in top] == sorted(total.items(), key=lambda wk: (-wk[1], wk[0]))[:150]


def big_table(n: int = 60_000):
    """The Comments table with ``n`` rows (more than a table works out in one go), sorted by sentiment."""
    QApplication.instance() or QApplication([])
    labels = ("positive", "neutral", "negative")
    rows = [{"id": f"c{i}", "sentiment": ((i * 7919) % 2001 - 1000) / 1000, "label": labels[i % 3],
             "body": f"Comment {i} about the {'Rogue' if i % 5 == 0 else 'mage'}\nclass", "score": (i * 31) % 997,
             "post_title": "a post", "author": f"user{i % 300}", "created_utc": 1_700_000_000.0 + i}
            for i in range(n)]
    tab = TablePage(TABLE_COMMENTS, stretch_col=2)
    tab.load(rows)
    return tab, rows


def test_sorting_a_big_table_is_worked_out_between_frames(monkeypatch):
    tab, rows = big_table()
    before = list(tab.model.rows)  # by sentiment
    sorts = []
    real = sorted
    monkeypatch.setattr(tables, "sorted", lambda items, **k: (sorts.append(len(items)), real(items, **k))[1],
                        raising=False)
    # This table's own threads: tables other tests left open start threads of their own as events are pumped.
    threads, real_on_thread = [], tables.on_thread

    def on_thread(work):
        caller = sys._getframe(1)
        if caller.f_locals.get("self") is tab.model:
            threads.append(caller.f_code.co_name)
        return real_on_thread(work)
    monkeypatch.setattr(tables, "on_thread", on_thread)
    tab.view.sortByColumn(3, Qt.SortOrder.DescendingOrder)  # Score
    # Sorting all of the owner's 650,000 comments at the click held the window a second and a half; now numbers are
    # sorted by numpy on a thread, no frame sorting anything, and the table shows the rows in their new order
    # once it's worked out.
    assert max(sorts, default=0) <= tab.model.STEP and tab.model._slices.running()
    until(lambda: not tab.model._slices.running(), 30)
    assert max(sorts, default=0) <= tab.model.STEP and len(threads) == 1, threads
    expected = real(before, key=lambda r: r["score"], reverse=True)
    assert [r["id"] for r in tab.model.rows] == [r["id"] for r in expected]  # as one stable sort orders them


def test_a_big_table_takes_the_rows_a_fetch_adds_while_it_sorts():
    tab, rows = big_table()
    tab.view.sortByColumn(3, Qt.SortOrder.DescendingOrder)
    new = [dict(rows[0], id="new-top", score=5000), dict(rows[0], id="new-low", score=-5)]
    assert tab.add_rows(new) == 2
    until(lambda: not tab.model._slices.running(), 30)
    shown = [r["id"] for r in tab.model.rows]
    assert shown[0] == "new-top" and shown[-1] == "new-low" and len(shown) == 60_002
    scores = [r["score"] for r in tab.model.rows]
    assert scores == sorted(scores, reverse=True)


def test_filtering_a_big_table_is_worked_out_between_frames_and_narrowed_as_you_type(monkeypatch):
    tab, rows = big_table()
    tab.view.selectionModel().select(tab.model.index(10, 0), QItemSelectionModel.SelectionFlag.Select
                                     | QItemSelectionModel.SelectionFlag.Rows)
    picked = tab.model.rows[10]["id"]
    texts = []
    real = TablePage._picked
    monkeypatch.setattr(TablePage, "_picked", lambda self, at, label, needle: (
        texts.append(len(at)) if needle else None, real(self, at, label, needle))[1])
    tab.filter.setText("rogue")
    # Going through every comment's text at the keystroke held the window 6 seconds with the owner's comments.
    assert max(texts, default=0) <= tab.model.STEP and tab.model.rowCount() == len(rows)
    until(lambda: not tab._slices.running(), 30)
    expected = {r["id"] for r in rows if "rogue" in r["body"].lower()}
    assert {r["id"] for r in tab.model.rows} == expected
    texts.clear()
    tab.filter.setText("rogue\n  CLASS")  # longer: only the rows "rogue" let through are looked at again
    until(lambda: not tab._slices.running(), 30)
    assert sum(texts) == len(expected)
    assert {r["id"] for r in tab.model.rows} == expected  # line breaks and runs of spaces count as one
    if picked in expected:
        assert [tab.model.row_at(i.row())["id"] for i in tab.view.selectionModel().selectedRows()] == [picked]
    tab.label_filter.setCurrentText("Negative")
    until(lambda: not tab._slices.running(), 30)
    assert {r["id"] for r in tab.model.rows} == {i for i in expected if int(i[1:]) % 3 == 2}


def test_a_big_table_loading_between_frames_takes_rows_a_fetch_adds_meanwhile():
    tab, rows = big_table(10)
    more = [dict(rows[0], id=f"m{i}", sentiment=0.5) for i in range(60_000)]
    steps = tab.load_steps(more)
    next(steps)  # the window's span starts it
    tab.add_rows([dict(rows[0], id="late", sentiment=0.25)])
    for _ in steps:
        pass
    assert tab.model.rowCount() == 60_001 and "late" in {r["id"] for r in tab.model.rows}
    assert len(tab.all_rows) == 60_001


def test_closing_the_window_while_a_big_subreddit_is_read_stops_the_read(tmp_path, monkeypatch):
    """Qt aborted the process on exit when the window closed with the read's thread still going (half a minute
    for the owner's 650,000 comments)."""
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    QSettings().setValue("ui/last_dataset", "Big")
    real = store_module.words_in
    monkeypatch.setattr(store_module, "words_in", lambda text: (time.sleep(0.001), real(text))[1])  # ~20 s to read
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)
    w = mw.MainWindow(db)
    w.show()
    until(lambda: w.load_job.running())
    QTest.qWait(200)
    started = time.monotonic()
    w.close()
    assert not w.load_job.running() and time.monotonic() - started < 2


def test_a_big_subreddit_is_read_as_arrays_that_count_what_its_rows_do(tmp_path):
    """Opening the owner's r/classicwow showed nothing for half a minute: every row was read, and its words split, before
    anything showed. Its RowIndex reads each day's numbers and word counts instead, in under a second."""
    db = str(tmp_path / "big.db")
    fill(db)
    got = []
    worker = LoadWorker(db, "Big")
    for name in ("read", "finished"):
        getattr(worker, name).connect(lambda payload, name=name: got.append((name, payload)))
    worker.run()
    assert [name for name, _ in got] == ["read", "finished"]
    saved = got[0][1][1]
    store = Store(db)
    for kind, rows in (("posts", store.posts("Big")), ("comments", store.comments("Big"))):
        for least in (None, 10):
            counted, expected = saved[kind].analysis(least), Analysis(scoring_at_least(rows, least))
            assert counted.counts == expected.counts
            assert {d: (n, labels) for d, (_, n, labels) in counted.days.items()} == {
                d: (n, labels) for d, (_, n, labels) in expected.days.items()}
            assert abs(counted.total - expected.total) < 1e-6
    assert len(saved["comments"]) == COMMENTS and saved["comments"]._drawn == {}  # no row read yet


def test_opening_a_big_subreddit_shows_its_overview_rows_and_words(tmp_path, monkeypatch):
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    QSettings().setValue("ui/last_dataset", "Big")
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)
    w = mw.MainWindow(db)
    try:
        w.show()
        until(lambda: w.comments_tab.model.rowCount() and not w._span_slices.running() and not w.load_job.busy
              and w.word_tally.ready and w.word_tally.top(["comments"]), 60)
        assert w._counted.count == POSTS + COMMENTS
        fresh = WordCounts.top(WordCounts("posts", Store(db).posts("Big")), WordCounts("comments", Store(db).comments("Big")), n=150)
        assert w.word_tally.top(["posts", "comments"], 150) == fresh
        # Only the rows the table draws were read, on a thread: 128 comments took 400 ms to read from the owner's packs.
        model = w.comments_tab.model
        until(lambda: not model._reads.running(), 30)  # the first and last rows, read as the table got them
        w._saved["comments"]._drawn.clear()
        reads = []
        real = Saved._read
        monkeypatch.setattr(Saved, "_read", lambda self, at: (reads.append(threading.current_thread()), real(self, at))[1])
        w.show_page("comments")
        until(lambda: model.data(model.index(0, 2)), 30)
        assert model.data(model.index(0, 2)) == tables._one_line(model.row_at(0)["body"])
        assert reads and threading.main_thread() not in reads
        assert 0 < len(w._saved["comments"]._drawn) <= RowSource.KEEP < COMMENTS
    finally:
        w.close()


def test_a_stricter_score_filter_finds_its_rows_among_those_in_memory(tmp_path, monkeypatch):
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    QSettings().setValue("ui/last_dataset", "Big")
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)
    w = mw.MainWindow(db)
    try:
        w.show()
        until(lambda: w.comments_tab.model.rowCount() and not w._span_slices.running() and not w.load_job.busy, 60)
        reads = count_calls(monkeypatch, Store, "rows")
        pick = w.comment_score_pick
        pick.setCurrentIndex(pick.findText("Comments 10+"))
        until(lambda: not w._span_slices.running() and w.comments_tab.model.rowCount() < COMMENTS, 30)
        # The owner's comments were read again from the database for each change: 40 seconds.
        assert reads == []
        expected = [r for r in Store(db).comments("Big") if r["score"] is not None and r["score"] >= 10]
        assert sorted(r["id"] for r in w.comments) == sorted(r["id"] for r in expected)
        assert w._analysis["comments"].counts == Analysis(expected).counts
    finally:
        w.close()


@pytest.mark.skipif(not Search(sqlite3.connect(":memory:")).ready, reason="this SQLite has no contentless deletes (3.43)")
def test_filtering_a_big_subreddit_looks_its_words_up_on_a_thread_once_typing_pauses(tmp_path, monkeypatch):
    """Going through 650,000 comments' text took seconds; the search index finds a word's in a few milliseconds."""
    QApplication.instance() or QApplication([])
    db = str(tmp_path / "big.db")
    fill(db)
    monkeypatch.setattr(tables.RowsModel, "BIG", 10_000)  # (20,000 comments here)
    saved = Saved(db, "comments", "Big")
    tab = TablePage(TABLE_COMMENTS, stretch_col=2)
    tab.set_rows(saved, np.arange(len(saved)))
    searched = []
    real = Saved.search
    monkeypatch.setattr(Saved, "search", lambda self, typed: (searched.append((typed, threading.current_thread())),
                                                              real(self, typed))[1])
    monkeypatch.setattr(Saved, "read_values", lambda self, key, step=2000: pytest.fail("every row's text was read"))
    for typed in ("g", "gr", "gre", "great"):
        tab.filter.setText(typed)
    until(lambda: searched and not tab._typing.isActive() and not tab._slices.running(), 30)
    assert [typed for typed, _ in searched] == ["great"]  # once typing paused
    assert threading.main_thread() not in [thread for _, thread in searched]
    db_ = Store(db)
    expected = {r["id"] for r in db_.comments("Big") if "great" in r["body"].lower()}
    db_.close()
    assert expected and {r["id"] for r in tab.model.rows} == expected
    tab.label_filter.setCurrentText("Negative")
    until(lambda: not tab._typing.isActive() and not tab._slices.running(), 30)
    assert {r["id"] for r in tab.model.rows} == {i for i in expected if saved.rows(saved.where([i]))[0]["label"] == "negative"}
    saved.close()


def test_a_row_deleted_after_its_subreddit_was_read_draws_as_gone_and_the_rest_as_they_are(tmp_path):
    db = str(tmp_path / "big.db")
    fill(db)
    saved = Saved(db, "comments", "Big")
    gone, kept = saved.ids[5].decode(), saved.ids[6].decode()
    store = Store(db)
    store.db.execute("DELETE FROM comments WHERE id = ?", (gone,))
    store.db.commit()
    store.close()
    first, second = saved.rows([5, 6])  # (the second came back as the first's)
    assert first["id"] == gone and first["body"] is None and second["id"] == kept and second["body"]
    saved.close()


def test_rows_read_for_drawing_while_a_fetch_adds_others_are_kept(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    db = str(tmp_path / "big.db")
    fill(db)
    saved = Saved(db, "comments", "Big")
    tab = TablePage(TABLE_COMMENTS, stretch_col=2)
    tab.set_rows(saved, np.arange(len(saved)))
    until(lambda: not tab.model._reads.running(), 30)  # the first and last rows, read as the table got them
    real = Saved._read

    reading = threading.Event()

    def slow(self, at):
        got = real(self, at)
        if threading.current_thread() is not threading.main_thread():
            reading.set()
            time.sleep(0.2)
        return got
    added = dict(saved.rows([COMMENTS - 1])[0], id="new1")
    saved._drawn.clear()
    monkeypatch.setattr(Saved, "_read", slow)
    model = tab.model
    assert model.data(model.index(0, 2)) is None  # drawn blank, read on a thread
    until(reading.is_set, 30)
    saved.add([added])  # a fetch saves a comment while the rows are read
    until(lambda: not model._reads.running(), 30)
    assert model.data(model.index(0, 2)) == tables._one_line(saved.rows([int(model.order[0])])[0]["body"])
    assert saved.kept(int(model.order[0])) is not None
    saved.close()


def test_rows_a_fetch_rescored_out_while_a_big_table_is_filtered_stay_out(monkeypatch):
    tab, rows = big_table()
    tab.filter.setText("rogue")
    QApplication.processEvents()  # under way
    assert tab._slices.running()
    keep = np.flatnonzero(np.arange(len(rows)) % 2 == 0)  # the odd ones went below the score filter
    tab.retain(keep)
    until(lambda: not tab._slices.running(), 30)
    assert {r["id"] for r in tab.model.rows} == {rows[i]["id"] for i in keep if "rogue" in rows[i]["body"].lower()}


def test_a_big_subreddit_that_failed_to_read_on_a_thread_is_read_again_here(tmp_path, monkeypatch):
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)
    w = mw.MainWindow(db)
    try:
        w.show()
        until(lambda: w.current == "Big" and not w.load_job.busy, 30)
        w._wanted = "big"
        w._on_load_failed("disk I/O error")
        assert w.current == "Big" and w._wanted is None and len(w._saved["comments"]) == COMMENTS
        w._loaded["big"] = w._loaded.get("big")
        w.days_deleted("Big", 1, 0, 0)
        assert "big" not in w._loaded
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_subreddit_opened_while_the_database_is_upgraded_has_its_words_counted_once_that_is_done(tmp_path, monkeypatch):
    """The first start on a 0.4 database read every comment's words out of its old packs while the upgrade
    rewrote them: the owner's 650,000 comments showed after three minutes. They're shown at once now, their words
    once the upgrade has counted them."""
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    old = sqlite3.connect(db)
    old.execute("PRAGMA user_version = 2")  # saved by 0.4, before packs counted their words
    old.close()
    QSettings().setValue("ui/last_dataset", "Big")
    done = threading.Event()
    monkeypatch.setattr(Store, "upgrade", classmethod(lambda cls, path, stop=None: done.wait(30)))
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)
    w = mw.MainWindow(db)
    try:
        w.show()
        until(lambda: w.current == "Big" and w.comments_tab.model.rowCount() == COMMENTS and not w.load_job.busy, 30)
        assert not w._saved["comments"].words and w.upgrading.is_alive()
        done.set()
        until(lambda: w._saved["comments"].words and not w.load_job.busy and w.word_tally.ready
              and w.word_tally.top(["comments"]), 30)
        assert w.comments_tab.model.rowCount() == COMMENTS
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)
