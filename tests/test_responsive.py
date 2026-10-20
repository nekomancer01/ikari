"""The window keeps answering while a big subreddit is zoomed, dragged and switched: a gesture only moves the
chart, and what follows the span (the tables, the word clouds, the background's tint) catches up once it
settles, a step a frame. Work is counted (paints, recounts, reads), not timed, so these hold on a slow or busy
runner too; tools/perf.py measures the frame times themselves."""
import os
import sqlite3
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment import analysis  # noqa: E402
from subsentiment.analysis import WordCounts  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import motion  # noqa: E402
from subsentiment.gui.charts import TimelineChart  # noqa: E402
from subsentiment.gui.words import WordCloud, WordTally  # noqa: E402
from subsentiment.store import Store  # noqa: E402

from test_big_data import COMMENTS, count_calls, fill  # noqa: E402


def until(done, seconds: float = 10) -> None:
    end = time.monotonic() + seconds
    while not done():
        assert time.monotonic() < end
        QApplication.processEvents()
        time.sleep(0.002)


def other(path) -> None:
    """A second subreddit as big as the first: its rows copied under new ids."""
    db = sqlite3.connect(path)
    for table, owner in (("posts", None), ("comments", "post_id")):
        cols = [r[1] for r in db.execute(f"PRAGMA table_info({table})")]
        rows = [dict(zip(cols, r)) for r in db.execute(f"SELECT * FROM {table}")]
        for r in rows:
            r["id"], r["subreddit"] = "o" + r["id"], "Other"
            if owner:
                r[owner], r["parent_id"] = "o" + r[owner], "t3_o" + r[owner]
        db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' * len(cols))})", [list(r.values()) for r in rows])
    db.commit()
    db.close()


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    db = str(tmp_path / "big.db")
    fill(db)
    other(db)
    w = mw.MainWindow(db)
    w.resize(1280, 760)
    w.show()
    w._refresh_datasets("Big")
    until(lambda: w.current == "Big" and not w._span_slices.running() and w.word_tally.ready)
    w.overview.words.placed()  # the cloud as first shown
    yield w
    w.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def wheel(chart, dy: int) -> None:
    pos = QPointF(chart._areas()[0].center())
    QApplication.sendEvent(chart, QWheelEvent(pos, chart.mapToGlobal(pos), QPoint(), QPoint(0, dy),
                                              Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                              Qt.ScrollPhase.NoScrollPhase, False))


def test_zooming_only_moves_the_chart_and_the_rest_catches_up_once_it_settles(window, monkeypatch):
    w = window
    chart = w.overview.timeline
    loads = count_calls(monkeypatch, type(w.comments_tab), "load")
    recounts = count_calls(monkeypatch, WordTally, "count")
    places = count_calls(monkeypatch, WordCloud, "_place")
    split, real_words_in = [], analysis.words_in
    monkeypatch.setattr(analysis, "words_in", lambda text: split.append(1) or real_words_in(text))
    for _ in range(12):  # twelve wheel steps, each painted
        wheel(chart, 120)
        chart.repaint()
        assert w.mood._anim.state() != w.mood._anim.State.Running  # no tint easing across the window
    assert chart.view is not None
    assert loads == [] and recounts == [] and places == []  # nothing but the chart and the tiles moved
    w._span_settle()  # what the settle timer does
    until(lambda: not w._span_slices.running() and w.word_tally.ready)
    assert len(loads) == 2 and len(recounts) == 1
    assert len(w.comments) == w.comments_tab.model.rowCount() < COMMENTS
    # Each row's text was split into words when the subreddit opened; the new span only adds them up.
    assert split == []
    whole = WordCounts.top(WordCounts("posts", w.posts), WordCounts("comments", w.comments), n=150)
    assert w.word_tally.top(["posts", "comments"], 150) == whole


def test_hovering_and_fades_repaint_a_picture_of_the_chart(window, monkeypatch):
    chart = window.overview.timeline
    chart.repaint()
    chart.repaint()  # still: kept as a picture from here on
    drawn = count_calls(monkeypatch, TimelineChart, "_paint_chart")
    plot = chart._areas()[0]
    for i in range(30):
        pos = QPointF(plot.left() + 20 * i, plot.center().y())
        QApplication.sendEvent(chart, QMouseEvent(QMouseEvent.Type.MouseMove, pos, chart.mapToGlobal(pos),
                                                  Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                                  Qt.KeyboardModifier.NoModifier))
        chart.repaint()
    assert chart._hover is not None and drawn == []


def test_new_points_from_a_fetch_dont_crossfade_the_chart(window):
    chart = window.overview.timeline
    chart.set_points([(d, mean + 0.1, n) for d, mean, n in chart.points])
    assert not motion.fading(chart)


def reads_of(monkeypatch, store) -> list:
    """Calls to this one connection's comments(), not a thread's own."""
    calls, real = [], store.comments
    monkeypatch.setattr(store, "comments", lambda *a: calls.append(1) or real(*a))
    return calls


def test_switching_back_to_a_subreddit_shows_it_from_memory(window, monkeypatch):
    w = window
    w._show_dataset("Other", wait=True)
    reads = reads_of(monkeypatch, w.store)
    w._show_dataset("Big")
    until(lambda: not w._span_slices.running())
    assert reads == [] and w.current == "Big" and w.comments_tab.model.rowCount() == COMMENTS


def test_a_big_subreddit_opens_on_a_thread_and_the_last_one_picked_wins(window, monkeypatch):
    w = window
    monkeypatch.setattr(mw.MainWindow, "LOAD_THREAD_FROM", 0)
    w._loaded.clear()
    reads = reads_of(monkeypatch, w.store)
    w._show_dataset("Other")
    assert w.current == "Big" and w.load_job.busy  # still showing Big while Other is read
    w._show_dataset("Big")  # changed their mind before it was in
    until(lambda: w.current == "Big" and not w.load_job.busy and w._wanted is None)
    until(lambda: not w._span_slices.running())
    assert w.current == "Big" and w.comments_tab.model.rowCount() == COMMENTS
    assert reads == []  # read with the thread's own connection, not the window's
    w._show_dataset("Other")
    until(lambda: w.current == "Other")
    until(lambda: not w._span_slices.running())
    assert {w.comments_tab.model.row_at(i)["subreddit"] for i in range(w.comments_tab.model.rowCount())} == {"Other"}


def test_moving_the_span_counts_only_the_rows_it_gained_and_lost():
    QApplication.instance() or QApplication([])
    rows = [{"id": f"c{i}", "body": f"river {'market' if i % 2 else 'bridge'} {i}", "label": "positive"}
            for i in range(3000)]
    tally = WordTally()
    tally.count([], rows[:2000])
    until(lambda: tally.ready)
    tally.count([], rows[500:2500])  # a span moved along: 500 rows off, 500 on
    assert tally.ready  # at once
    fresh = WordCounts("comments", rows[500:2500])
    assert tally.top(["comments"], 50) == WordCounts.top(fresh, n=50)
