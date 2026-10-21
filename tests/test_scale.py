"""A big saved subreddit (Ame's has 650,000 comments) opens without holding up the window or filling memory. Work
and memory are counted, not timed, so these hold on a slow runner too; tools/perf.py --db measures the frame times
on a real database."""
import os
import time
import tracemalloc
from collections import Counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment.analysis import Analysis, WordCounts  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import tables  # noqa: E402
from subsentiment.gui.tables import COMMENT_COLUMNS as TABLE_COMMENTS, TablePage  # noqa: E402
from subsentiment.gui.words import WordTally  # noqa: E402
from subsentiment.gui.worker import LoadWorker  # noqa: E402
from subsentiment import store as store_module  # noqa: E402
from subsentiment.store import Store  # noqa: E402

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
        # Reading Ame's 650,000 comments took 17 s before the window first showed.
        assert reads == [[], [], []] and w.subheading.text() == "Opening r/Big…"
        w.show()
        until(lambda: len(w._saved["comments"]) == COMMENTS and not w._span_slices.running(), 30)
        assert w.current == "Big" and w.comments_tab.model.rowCount() == COMMENTS
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_big_subreddit_takes_half_the_memory_it_did(tmp_path):
    db = str(tmp_path / "big.db")
    fill(db)
    got = []
    worker = LoadWorker(db, "Big", (None, None))
    worker.read.connect(got.append)
    worker.finished.connect(got.append)  # each row's words: kept as long as its rows, so counted with them
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        worker.run()  # as the window reads it, on this thread instead of its own
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    (_, rows, analysis), (_, words) = got
    assert len(words["comments"]) == COMMENTS
    assert len(rows["comments"]) == COMMENTS and analysis["comments"].count == COMMENTS
    # A dict per row took about 1,040 bytes a row here; a tuple of its values, with what rows share (post ids and
    # titles, labels, authors) kept once, about 530.
    assert held / (POSTS + COMMENTS) < 700


class Counted(dict):
    """A row that counts how often its id is read."""
    reads = 0

    def __getitem__(self, key):
        if key == "id":
            Counted.reads += 1
        return super().__getitem__(key)


def test_moving_the_span_over_a_big_subreddit_works_out_what_changed_between_frames():
    QApplication.instance() or QApplication([])
    rows = [Counted(id=f"c{i}", body=f"river {'market' if i % 3 else 'bridge'} lane{i % 50}",
                    label=("positive", "negative", "neutral")[i % 3]) for i in range(90_000)]
    tally = WordTally()
    tally.count([], rows[:60_000])
    until(lambda: tally.ready and not tally._slices.running(), 30)
    Counted.reads = 0
    tally.count([], rows[1_000:61_000])  # the span moved along a day or so
    # Main went through every row of the span at once to see which were new (most of a second with Ame's
    # 650,000 comments); it's done between frames now, the counts shown meanwhile.
    assert Counted.reads == 0 and tally.ready
    until(lambda: tally.ready and not tally._slices.running(), 30)
    assert tally.top(["comments"], 60) == WordCounts.top(WordCounts("comments", rows[1_000:61_000]), n=60)


def test_a_comment_fetched_while_the_span_moves_stays_in_the_cloud():
    QApplication.instance() or QApplication([])
    rows = [{"id": f"c{i}", "body": f"river lane{i % 50}", "label": "neutral"} for i in range(30_000)]
    fetched = {"id": "new", "body": "zebraword", "label": "positive"}
    for moved in (100, 5_000):  # a small move, worked out between frames; a big one, counted afresh
        tally = WordTally()
        tally.count([], rows[:25_000])
        until(lambda: tally.ready and not tally._slices.running(), 30)
        tally.count([], rows[moved:25_000 + moved])
        tally.add("comments", [fetched])  # saved by a running fetch before the move is worked out
        until(lambda: tally.ready and not tally._slices.running(), 30)
        assert tally.counts["comments"].counts["positive"]["zebraword"] == 1, moved


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
    tab.view.sortByColumn(3, Qt.SortOrder.DescendingOrder)  # Score
    # Sorting all of Ame's 650,000 comments at the click held the window a second and a half; now no frame
    # sorts more than a step's rows, and the table shows them in their new order once it's worked out.
    assert max(sorts, default=0) <= tab.model.STEP
    until(lambda: not tab.model._slices.running(), 30)
    assert max(sorts) <= tab.model.STEP
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
    real = tables.column
    monkeypatch.setattr(tables, "column", lambda rs, key: (texts.append(len(rs)) if key == "body" else None,
                                                           real(rs, key))[1])
    tab.filter.setText("rogue")
    # Going through every comment's text at the keystroke held the window 6 seconds with Ame's comments.
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
    for Ame's 650,000 comments)."""
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


def test_a_big_subreddit_is_summed_up_by_the_store_before_its_rows_are_read_and_its_words_split_meanwhile(tmp_path):
    """Opening Ame's r/classicwow showed nothing for half a minute: every row was read, and its words split, before
    the overview had anything to count. Its days are now counted by the store first (a second), and the words
    split in other processes while the rows are read."""
    db = str(tmp_path / "big.db")
    fill(db)
    got = []
    worker = LoadWorker(db, "Big", (None, 10))
    for name in ("summed", "read", "finished"):
        getattr(worker, name).connect(lambda payload, name=name: got.append((name, payload)))
    worker.run()
    assert [name for name, _ in got] == ["summed", "read", "finished"]
    sums, (rows, counted), (_, words) = got[0][1][1], got[1][1][1:], got[2][1]
    for kind in ("posts", "comments"):
        assert sums[kind].counts == counted[kind].counts
        assert {d: (n, labels) for d, (_, n, labels) in sums[kind].days.items()} == {
            d: (n, labels) for d, (_, n, labels) in counted[kind].days.items()}
        assert abs(sums[kind].total - counted[kind].total) < 1e-6
    assert all(r.words is None for r in rows["comments"])
    split = Store(db).rows("comments", "Big", words=True)
    # Every comment's, whatever the score filter (in any order: another process orders a set its own way).
    assert {i: set(w) for i, w in words["comments"].items()} == {r.id: set(r.words) for r in split}


def test_opening_a_big_subreddit_shows_its_overview_then_its_rows_then_its_words(tmp_path, monkeypatch):
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    QSettings().setValue("ui/last_dataset", "Big")
    monkeypatch.setattr(mw.MainWindow, "wait_for_data", False)
    seen = []
    w = mw.MainWindow(db)
    try:
        w.show()

        def look():
            state = (w._counted.count > 0, len(w._saved["comments"]) > 0,
                     w.word_tally.ready and bool(w.word_tally.counts["comments"].ids()))
            if not seen or seen[-1] != state:
                seen.append(state)
            return (state == (True, True, True) and w.comments_tab.model.rowCount() and not w._span_slices.running()
                    and not w.load_job.busy)
        until(look, 60)
        assert (True, False, False) in seen  # the overview before the rows were in
        assert w._counted.count == POSTS + COMMENTS
        fresh = WordCounts.top(WordCounts("posts", Store(db).posts("Big")), WordCounts("comments", Store(db).comments("Big")), n=150)
        assert w.word_tally.top(["posts", "comments"], 150) == fresh
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
        reads = [count_calls(monkeypatch, Store, name) for name in ("rows", "day_totals")]
        pick = w.comment_score_pick
        pick.setCurrentIndex(pick.findText("Comments 10+"))
        until(lambda: not w._narrow_slices.running() and not w._span_slices.running()
              and w.comments_tab.model.rowCount() < COMMENTS, 30)
        # Ame's comments were read again from the database for each change: 40 seconds.
        assert reads == [[], []]
        expected = [r for r in Store(db).comments("Big") if r["score"] is not None and r["score"] >= 10]
        assert sorted(r["id"] for r in w.comments) == sorted(r["id"] for r in expected)
        assert w._analysis["comments"].counts == Analysis(expected).counts
    finally:
        w.close()
