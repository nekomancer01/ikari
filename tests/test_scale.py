"""A big saved subreddit (Ame's has 650,000 comments) opens without holding up the window or filling memory. Work
and memory are counted, not timed, so these hold on a slow runner too; tools/perf.py --db measures the frame times
on a real database."""
import os
import tracemalloc
from collections import Counter

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment.analysis import WordCounts  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui.words import WordTally  # noqa: E402
from subsentiment.gui.worker import LoadWorker  # noqa: E402
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
    worker.finished.connect(got.append)
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        worker.run()  # as the window reads it, on this thread instead of its own
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    _, rows, analysis = got[0]
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
