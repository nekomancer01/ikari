"""The window keeps answering while a big subreddit is zoomed, dragged and switched: a gesture only moves the
chart, and what follows the span (the tables, the word clouds, the background's tint) catches up once it
settles, a step a frame. Work is counted (paints, recounts, reads), not timed, so these hold on a slow or busy
runner too; tools/perf.py measures the frame times themselves."""
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent, QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from subsentiment import analysis  # noqa: E402
from subsentiment.analysis import WordCounts  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import motion, theme  # noqa: E402
from subsentiment.gui.charts import TimelineChart  # noqa: E402
from subsentiment.gui.rows import Saved  # noqa: E402
from subsentiment.gui.words import KINDS, WordCloud, WordTally  # noqa: E402
from subsentiment.reddit import Comment, Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

from test_big_data import COMMENTS, POSTS, count_calls, fill  # noqa: E402


def until(done, seconds: float = 10, doing=lambda: "") -> None:
    end = time.monotonic() + seconds
    while not done():
        names = {t.ident: t.name for t in threading.enumerate()}
        assert time.monotonic() < end, doing() + "".join(  # where each thread was (QThreads too) when it timed out
            f"\n--- {names.get(ident, ident)}\n" + "".join(traceback.format_stack(frame))
            for ident, frame in sys._current_frames().items())
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
    loads = count_calls(monkeypatch, type(w.comments_tab), "set_rows_steps")  # the span loads the tables a step at a time
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
    # Each day's words were counted when they were saved; the new span only adds them up.
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


def test_a_window_edge_dragged_places_the_word_cloud_once_the_size_holds_still(window, monkeypatch):
    w = window
    cloud = w.overview.words
    cloud.repaint()
    places = count_calls(monkeypatch, WordCloud, "_place")
    before = cloud.size()
    for i in range(1, 11):  # a step a frame
        w.resize(1280 - 12 * i, 760 - 6 * i)
        QApplication.processEvents()
        w.repaint()
    assert places == [] and cloud.size() != before  # the words as placed, centred in the cloud meanwhile
    box, word = cloud._placed[0][:2]
    assert cloud.word_at(box.center() + QPointF(*cloud._offset())) == word
    until(lambda: not cloud._resizing.isActive() and cloud._placing is None
          and cloud._placed_for[:2] == (cloud.width(), cloud.height()))
    assert len(places) == 1 and cloud._offset() == (0, 0)
    assert all(QRectF(cloud.rect()).contains(b[0]) for b in cloud.placed())


def test_a_range_button_eases_the_chart_before_the_tables_and_words_catch_up(window, monkeypatch):
    w = window
    chart = w.overview.timeline
    easing = []  # whether the chart was still easing as each table load or word count started
    for cls, name in ((type(w.comments_tab), "set_rows_steps"), (WordTally, "count")):
        real = getattr(cls, name)
        monkeypatch.setattr(cls, name, lambda self, *a, real=real, **k: easing.append(chart.zoom.running())
                            or real(self, *a, **k))
    key = next(k for k, b in chart.presets.items() if b.isEnabled() and k not in ("All", chart.preset))
    chart.presets[key].click()
    assert chart.zoom.running()
    until(lambda: len(easing) >= 3 and not w._span_slices.running() and w.word_tally.ready)
    assert not any(easing)


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


def saved_rows(path, posts=(), comments=()) -> dict:
    """Subreddit "town" with these posts and comments saved (dicts: id, text, label, day), as the window reads it."""
    s = Store(path)
    for p in posts:
        s.save_post(Post(p["id"], "town", p["text"], "", "a", 1, 0.9, 0, p["day"], "https://r", ""), 0.0, p["label"])
    for c in comments:
        s.save_comment(Comment(c["id"], "p", "town", "t3_p", "b", c["text"], 1, c["day"], 0, "/r/x/"), 0.0, c["label"])
    s.commit()
    s.close()
    return {k: Saved(str(path), k, "town") for k in KINDS}


def test_moving_the_span_counts_its_days_words_without_splitting_any_text_again(tmp_path, monkeypatch):
    QApplication.instance() or QApplication([])
    rows = [{"id": f"c{i}", "text": f"river {'market' if i % 2 else 'bridge'} {i}", "label": "positive",
             "day": 1_700_000_000.0 + i * 600} for i in range(3000)]
    saved = saved_rows(tmp_path / "w.db", comments=rows)
    tally = WordTally()
    tally.count(saved, None, (None, None))
    until(lambda: tally.ready)
    split, real_words_in = [], analysis.words_in
    monkeypatch.setattr(analysis, "words_in", lambda text: split.append(1) or real_words_in(text))
    span = (int(rows[500]["day"] // 86400 + 1) * 86400, int(rows[2500]["day"] // 86400) * 86400)
    tally.count(saved, span, (None, None))  # a span moved along
    until(lambda: tally.ready)
    assert split == []
    fresh = WordCounts("comments", [dict(r, body=r["text"]) for r in rows if span[0] <= r["day"] < span[1]])
    assert tally.top(["comments"], 50) == WordCounts.top(fresh, n=50)


def test_rows_saved_again_with_a_new_label_are_counted_again(tmp_path):
    QApplication.instance() or QApplication([])
    saved = saved_rows(tmp_path / "w.db", posts=[{"id": "a", "text": "happy banana", "label": "positive",
                                                  "day": 1_700_000_000.0}])
    tally = WordTally()
    tally.count(saved, None, (None, None))
    saved["posts"].add([{"id": "a", "title": "sad banana", "selftext": "", "label": "negative", "sentiment": -0.5,
                         "score": 1, "created_utc": 1_700_000_000.0}])
    tally.count(saved, None, (None, None))
    assert tally.top(["posts"]) == [("banana", 1, -1.0), ("sad", 1, -1.0)]


def test_a_theme_switch_restyles_only_the_window_and_fades_two_pictures(window, monkeypatch):
    # Restyling every widget the app has (a new app style sheet, or setting the style again) took ~0.4 s at
    # the owner's size, and each frame of the fade then repainted the whole window under the old look's picture.
    w, app = window, QApplication.instance()
    monkeypatch.setattr(motion, "_os_allows", True)
    monkeypatch.setattr(motion, "_wanted", True)
    w.show_page("overview")
    until(lambda: w.overview.timeline.isVisible())
    w._set_theme("dark")  # the first switch in a run sets the app's style; the app has done so at start
    until(lambda: not motion.fading(w))
    sheet, style = app.styleSheet(), app.style()
    painted = count_calls(monkeypatch, TimelineChart, "paintEvent")
    for key in ("light", "dark"):
        w._set_theme(key)
        until(lambda: not motion.fading(w))
    assert app.styleSheet() == sheet and app.style() is style
    assert w.styleSheet() == theme.stylesheet(theme.tokens())
    # Each switch paints the timeline once into the new look's picture, then once more when the fade is gone.
    assert len(painted) <= 4


def test_a_theme_switch_during_a_page_fade_doesnt_hold_that_fade_still(window, monkeypatch):
    w = window
    monkeypatch.setattr(motion, "_os_allows", True)
    monkeypatch.setattr(motion, "_wanted", True)
    w.show_page("words")  # crossfades the content area
    assert motion.fading(w.content)
    w._set_theme("dark")  # the theme's picture already has the page fade's frame; that fade stops
    assert not motion.fading(w.content)


def test_a_theme_switch_and_reduce_transparency_restyle_no_more_than_they_change(window):
    """A restyle takes Qt about 0.2 ms a widget at 150%, so: Reduce transparency (which only changes how the
    backdrop paints) restyles nothing, and the outlines' filter sees only the widgets that can take an outline,
    not every event of every widget in the app."""
    from PySide6.QtCore import QEvent, QObject
    from PySide6.QtWidgets import QLabel, QPushButton
    w = window
    restyled = []

    class Watch(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.StyleChange:
                restyled.append(obj)
            return False
    watch = Watch()
    w._set_theme("light")
    QApplication.processEvents()
    label = next(x for x in w.findChildren(QLabel) if x.isVisible() and x.objectName() != "motion-snapshot")
    button = next(x for x in w.findChildren(QPushButton) if x.isVisible())
    label.installEventFilter(watch)
    w._set_reduce(True)
    QApplication.processEvents()
    assert not restyled
    w._set_reduce(False)
    w._set_theme("dark")
    QApplication.processEvents()
    assert restyled  # the theme does restyle
    until(lambda: not motion.fading(w))  # (nothing under its still paints)
    seen = []  # what Outline's filter is called with (Qt calls it, so it's watched from Python's side)
    code = theme.Outline.eventFilter.__code__
    sys.setprofile(lambda frame, what, arg: seen.append(frame.f_locals["obj"])
                   if what == "call" and frame.f_code is code else None)
    try:
        label.repaint()
        button.repaint()
    finally:
        sys.setprofile(None)
    assert label not in seen and button in seen


def test_a_wait_shows_the_throbber_only_once_it_has_gone_on_for_a_second(monkeypatch):
    from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget
    from subsentiment.gui import widgets
    QApplication.instance() or QApplication([])
    now = [100.0]
    monkeypatch.setattr(widgets.time, "monotonic", lambda: now[0])
    host = QWidget()
    host.resize(900, 120)
    lay = QVBoxLayout(host)
    beside = widgets.ElidedLabel("12 posts and 40 comments", "subtitle")
    rule = theme.mark(QFrame(), "role", "divider")
    lay.addWidget(beside)
    lay.addWidget(rule)
    waiting = [None]
    throbber = widgets.Throbber(rule, lambda: waiting[0], beside=beside)
    host.show()
    QApplication.processEvents()
    waiting[0] = "Counting words…"
    throbber.check()
    now[0] += 0.5
    throbber.check()
    assert throbber.isHidden()  # half a second in: nothing shown, so a quick action doesn't flicker
    waiting[0] = None
    throbber.check()
    waiting[0] = "Counting words…"
    now[0] += 0.7
    throbber.check()
    assert throbber.isHidden()  # the wait that ended doesn't count towards this one
    now[0] += 1.0
    throbber.check()
    assert throbber.isVisible() and throbber.caption.isVisible() and throbber.caption.text() == "Counting words…"
    g = rule.geometry()
    assert throbber.geometry().center().y() in (g.center().y() - 1, g.center().y(), g.center().y() + 1)
    assert throbber.caption.geometry().right() <= g.right() and throbber.caption.y() >= beside.y()
    beside.setText("x " * 400)  # text reaching the caption's place: the bar alone
    throbber.check()
    assert throbber.isVisible() and throbber.caption.isHidden()
    waiting[0] = None
    throbber.check()
    assert throbber.isHidden() and throbber.caption.isHidden()
    assert throbber._poll.isActive()
    host.hide()  # minimized or closed: nothing asked while it can't be seen
    assert not throbber._poll.isActive()
    host.close()


def test_opening_filtering_and_counting_a_big_subreddit_show_the_throbber_until_done(window, monkeypatch):
    w = window
    monkeypatch.setattr(w.throbber, "DELAY", 0.0)
    assert w.waiting() is None
    w.throbber.check()
    assert w.throbber.isHidden()
    monkeypatch.setattr(mw.MainWindow, "LOAD_THREAD_FROM", 0)
    w._loaded.clear()
    w._show_dataset("Other")
    seen = set()

    def watch():
        w.throbber.check()
        if w.throbber.isVisible():
            seen.add(w.waiting())
        return w.current == "Other" and w.waiting() is None
    until(watch, 30)
    assert "" in seen  # while it was read, the header saying so
    w.throbber.check()
    assert w.throbber.isHidden()
    w.comment_score_pick.setCurrentIndex(2)  # a stricter filter: found among the rows in memory, a slice a frame
    assert w.waiting() is not None
    until(watch, 30)
    w.throbber.check()
    assert w.throbber.isHidden()


def test_a_download_into_a_big_subreddit_hands_its_rows_to_the_window(window, monkeypatch):
    # The window read back every row the fetch saved on its own connection, a row at a time, each read waiting on
    # the fetch's thread for the interpreter lock: seconds of frozen window for a big thread's comments. Now the
    # fetch's thread reads them, its comments are scored in other processes, and a big table out of sight takes
    # them in when its page shows.
    from fake_archive import FakeArchive
    from subsentiment import arctic_shift, collect
    from subsentiment.gui import tables, worker
    w = window
    day = int(time.time() // 86400 * 86400) - 3 * 86400
    threads, per = 4, worker.ThreadScorer.LEAST + 20
    posts = [{"id": f"dl{k}", "subreddit": "Big", "title": f"thread {k}", "selftext": "", "author": "a", "score": 9,
              "upvote_ratio": 0.9, "num_comments": per, "created_utc": day + 600 * k, "retrieved_on": day + 2 * 86400,
              "permalink": f"/r/Big/comments/dl{k}/x/", "url": ""} for k in range(threads)]
    archive = FakeArchive(posts, rate=(30, 1.0))
    monkeypatch.setattr(arctic_shift, "BASE_URL", archive.url)
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 1.2)
    monkeypatch.setattr(worker, "LIMITER", arctic_shift.RateLimiter(arctic_shift.BASE_INTERVAL / 10))
    monkeypatch.setattr(tables.RowsModel, "BIG", 1000)  # these tables count as big ones
    read = []
    for name in ("posts_since", "comments_since"):
        real = getattr(w.store, name)
        monkeypatch.setattr(w.store, name, lambda *a, real=real: (rows := real(*a), read.append(len(rows)))[0])
    scored_here = count_calls(monkeypatch, worker.Lexicon, "score")
    filled = Store(w.db_path)
    filled.pack_settled()  # the 40,000 rows the window was filled with (seconds on Windows): the fetch packs its own
    filled.close()
    monkeypatch.setattr(worker.ThreadScorer, "broken", False)
    worker.ThreadScorer.start()  # up, as on a download long enough for them to come up (seconds, on Windows)
    until(worker.ThreadScorer.ready, 60)
    w.show_page("overview")
    shown = w.comments_tab.model.rowCount()
    try:
        assert w.start_request(collect.FetchRequest("Big", limit=None, days=(day,), include_comments=True,
                                                    comments_per_post=0), "Downloading")
        until(lambda: not w.fetch_job.busy, 60, lambda: w.statusBar().currentMessage())
    finally:
        archive.close()
        worker.ThreadScorer.close()
    saved = threads * (1 + per)
    assert len(w._saved["posts"]) + len(w._saved["comments"]) == POSTS + COMMENTS + saved
    assert sum(read) <= 2  # the end of the fetch looked for anything after the last rows sent: the last again at most
    assert len(scored_here) == threads  # its posts' titles; their comments went to other processes
    assert len(w.comments) == COMMENTS + threads * per  # counted in (the heading says so) but not put in the table
    assert w.comments_tab.model.rowCount() == shown
    w.show_page("comments")
    assert w.comments_tab.model.rowCount() == COMMENTS + threads * per


def test_new_points_from_a_fetch_are_painted_once_while_the_mouse_is_over_the_chart(window, monkeypatch):
    # Painted straight onto the widget, then again as a picture for the hover that followed: twice for each new
    # set of points, four times a second while a fetch ran.
    chart = window.overview.timeline
    chart.repaint()
    chart.repaint()
    drawn = count_calls(monkeypatch, TimelineChart, "_paint_chart")
    plot = chart._areas()[0]
    for k in range(3):
        chart.set_points([(d, mean, n + 1) for d, mean, n in chart.points])
        for i in range(4):
            pos = QPointF(plot.left() + 20 * (4 * k + i), plot.center().y())
            QApplication.sendEvent(chart, QMouseEvent(QMouseEvent.Type.MouseMove, pos, chart.mapToGlobal(pos),
                                                      Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                                                      Qt.KeyboardModifier.NoModifier))
            chart.repaint()
    assert len(drawn) == 3


def test_opening_a_big_subreddit_on_a_thread_leaves_the_garbage_collector_to_free_what_goes(window, monkeypatch):
    # Garbage is collected on the window's thread only (worker.Collector). Opening a big subreddit holds it off while
    # the thread reads, but froze everything alive at the end once: a closed window's widgets, and every opened
    # subreddit's connections once they were let go, then stayed for good.
    import gc
    import weakref
    w = window
    monkeypatch.setattr(mw.MainWindow, "LOAD_THREAD_FROM", 0)
    w._loaded.clear()
    frozen = gc.get_freeze_count()

    class Cycle:
        pass
    c = Cycle()
    c.me = c
    gone = weakref.ref(c)
    del c
    w._show_dataset("Other")
    until(lambda: w.current == "Other" and not w.load_job.busy)
    until(lambda: gone() is None)  # collected by the window's collector
    assert gc.get_freeze_count() == frozen


class Paints(QObject):
    """Counts a widget's paint events."""

    def __init__(self, widget):
        super().__init__()
        self.count = 0
        widget.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        self.count += event.type() == QEvent.Type.Paint
        return False


def test_a_page_fade_holds_the_new_page_as_a_picture_but_shows_what_eases_on_it(window, monkeypatch):
    # Every frame of a page's 220 ms fade repainted the whole page under the old look's still (25-100 ms a
    # click, 6-12 frames over budget). The new look is a picture now, grabbed again whole only now and then.
    w = window
    monkeypatch.setattr(motion, "_os_allows", True)
    monkeypatch.setattr(motion, "_wanted", True)
    w.show_page("posts")
    until(lambda: not motion.fading(w.content))
    paints = Paints(w.overview.timeline)
    w.show_page("overview")
    fade = w.content.findChild(QWidget, "motion-snapshot")
    for t in range(16, motion.VIEW_MS, 16):  # a frame at a time, quicker than the fade's own clock
        fade.anim.setCurrentTime(t)
        QApplication.processEvents()
    assert paints.count <= 3  # grabbed whole on the first frame, and again only once REGRAB_MS has passed
    until(lambda: not motion.fading(w.content))

    # A count easing under a fade is grabbed again on every frame, so it eases as it would without the fade:
    # the picture matches the window drawn afresh there.
    def drawn(widget, picture=None):
        window_ = widget.window()
        at = widget.mapTo(window_, QPoint(0, 0))
        if picture is None:  # the window drawn afresh, without the fade
            fade.hide()
            picture = window_.grab(fade.rect().translated(fade.parentWidget().mapTo(window_, QPoint(0, 0))
                                                          - fade._lead))
            fade.show()
        origin = fade.parentWidget().mapTo(window_, QPoint(0, 0)) - fade._lead
        dpr = picture.devicePixelRatio()
        return picture.copy(QRect((at - origin) * dpr, widget.size() * dpr)).toImage()
    w.show_page("words")
    until(lambda: not motion.fading(w.content))
    w.show_page("overview")
    fade = w.content.findChild(QWidget, "motion-snapshot")
    fade.anim.setCurrentTime(16)  # the first frame grabs the new look whole
    fade._grabbed = time.monotonic()  # and no whole grab after: only what moves
    count = w.overview.tiles["count"].value
    count._count.set(count._count.value + 5000)  # as a new span's count eases in
    count._count._anim.setCurrentTime(motion.DATA_MS // 2)
    fade.anim.setCurrentTime(32)
    assert drawn(count, fade._under) == drawn(count)


SCROLL_AT_150 = """
import sys, time
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QSettings, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QApplication
from subsentiment.gui import main_window as mw, motion, theme
app = QApplication([])
app.setOrganizationName("subsentiment-test")
app.setApplicationName("scroll-150")
theme.load_fonts(app)
theme.apply(app, "light")  # as the app starts: the style sheet makes the table see-through
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, sys.argv[2])
QSettings().setValue("updates/auto", False)
motion.set_enabled(False)
w = mw.MainWindow(sys.argv[1])
w.resize(1280, 760)  # 1920x1200 at 150%, restored
w.show()
def pump(done, seconds=20):
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
w.show_page("posts")
view, model = w.posts_tab.view, w.posts_tab.model
viewport = view.viewport()
pump(lambda: view.verticalScrollBar().maximum() > 6 and not w.posts_tab.busy())
pump(lambda: False, 1.5)  # the counts and the mood tint have eased in
class Paints(QObject):
    area = 0
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Paint:
            self.area += sum(r.width() * r.height() for r in event.region())
        return False
paints, under = Paints(), Paints()
viewport.installEventFilter(paints)
w.installEventFilter(under)  # the window's backdrop, under the table
pixels = lambda i: np.frombuffer(i.constBits(), np.uint8).reshape(i.height(), -1).astype(int)
print("dpr", w.devicePixelRatioF())
worst, shares = 0, []
for dy in (-120,) * 4 + (120,) * 2:
    paints.area = under.area = 0
    QApplication.sendEvent(viewport, QWheelEvent(QPointF(200, 60), QPointF(viewport.mapToGlobal(QPoint(200, 60))),
                                                 QPoint(), QPoint(0, dy), Qt.MouseButton.NoButton,
                                                 Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False))
    app.processEvents()
    # Before rows read on a thread are drawn: what the notch drew of the table, and of the window under it.
    shares.append(max(paints.area, under.area) / (viewport.width() * viewport.height()))
    pump(lambda: not model._reads.running() and not model._asked)
    app.processEvents()
    moved = pixels(w.backingStore().paintDevice().copy())
    w.repaint()  # all of it drawn again
    worst = max(worst, int(np.abs(moved - pixels(w.backingStore().paintDevice().copy())).max()))
print("worst", worst)
print("drawn", max(shares))
"""


def test_scrolling_a_table_moves_its_rows_and_draws_only_the_new_ones(tmp_path):
    # A wheel notch repainted the whole table, and the window under it (35-51 ms a notch at 150%): Qt moves the
    # pixels of a see-through widget only where it starts on a whole device pixel.
    db = str(tmp_path / "big.db")
    fill(db)
    screen = tmp_path / "screen.json"  # 150% as Windows sets it: a screen of 144 dpi
    screen.write_text('{"screens": [{"name": "s", "x": 0, "y": 0, "width": 1920, "height": 1200, "logicalDpi": 144, '
                      '"logicalBaseDpi": 96, "dpr": 1}]}')
    # Named from the folder it's in: Qt splits the platform's options at colons, which a Windows path has.
    run = subprocess.run([sys.executable, "-X", "faulthandler", "-c", SCROLL_AT_150, db, str(tmp_path / "cfg150")],
                         capture_output=True, text=True, timeout=180, cwd=tmp_path,
                         env=dict(os.environ, QT_QPA_PLATFORM=f"offscreen:configfile={screen.name}"))
    assert run.returncode == 0, run.stderr[-2000:]
    out = dict(line.split(" ", 1) for line in run.stdout.splitlines() if " " in line)
    assert out["dpr"] == "1.5", run.stdout
    assert int(out["worst"]) <= 1, run.stdout  # the same pixels as drawing it all again, but for a gradient's rounding
    assert float(out["drawn"]) < 0.5, run.stdout  # only the rows that came into view, not the whole table


CARDS_AT_150 = """
import sys, time
import numpy as np
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSettings, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget
from subsentiment.gui import main_window as mw, motion, theme
app = QApplication([])
app.setOrganizationName("subsentiment-test")
app.setApplicationName("cards-150")
theme.load_fonts(app)
theme.apply(app, "light")
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, sys.argv[2])
QSettings().setValue("updates/auto", False)
motion.set_enabled(False)
w = mw.MainWindow(sys.argv[1])
w.resize(1280, 760)
w.show()
def pump(done, seconds=20):
    end = time.monotonic() + seconds
    while not done() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)
pump(lambda: w.current and not w._span_slices.running() and w.word_tally.ready)
pump(lambda: False, 1.0)
print("dpr", w.devicePixelRatioF())
charts = (w.overview.timeline, w.overview.distribution, w.overview.words)
# A frame of the timeline alone (a zoom's) paints it and nothing under it.
class Painted(QObject):
    seen = []
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Type.Paint:
            self.seen.append(type(obj).__name__)
        return False
painted, x = Painted(), charts[0]
while x is not None:
    x.installEventFilter(painted)
    x = x.parentWidget()
pump(lambda: False, 0.3)
painted.seen.clear()
charts[0].update()
pump(lambda: painted.seen, 2)
pump(lambda: False, 0.1)
print("painted", ",".join(painted.seen))
pixels = lambda i: np.frombuffer(i.constBits(), np.uint8).reshape(i.height(), -1)[:, :4 * i.width()].astype(int)
def drawn():
    # Every pixel painted again over magenta: one left magenta is one nothing painted (a seam at an edge that falls
    # between device pixels).
    store = w.backingStore().paintDevice()
    p = QPainter(store)
    p.fillRect(store.rect(), QColor(255, 0, 255))
    p.end()
    for widget in [w, *w.findChildren(QWidget)]:
        widget.update()
    app.processEvents()
    image = w.backingStore().paintDevice()
    image = image.copy(0, 0, round(w.width() * 1.5), round(w.height() * 1.5))
    return pixels(image)
unpainted, worst = 0, 0
for page in ("overview", "words"):
    w.show_page(page)
    pump(lambda: False, 0.3)
    for i in range(6):  # edges on whole and half device pixels
        w.resize(1280 - 7 * i, 760 - 5 * i)
        cloud = w.overview.words if page == "overview" else w.words_page.body
        pump(lambda: not cloud._resizing.isActive() and cloud._placing is None  # the words placed for the size,
             and cloud._placed_for[:2] == (cloud.width(), cloud.height())  # and eased there
             and cloud._anim.state() != cloud._anim.State.Running)
        own = drawn()
        unpainted += int((own.reshape(own.shape[0], -1, 4) == (255, 0, 255, 255)).all(axis=2).sum())
        for c in charts:
            c.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        worst = max(worst, int(np.abs(own - drawn()).max()))  # with the card painted under them, as before
        for c in charts:
            c.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
print("unpainted", unpainted)
print("worst", worst)
"""


def test_the_charts_and_clouds_paint_their_card_so_a_frame_of_one_paints_nothing_under_it(tmp_path):
    # A zoom's frame repainted the window's backdrop, page, panel and card under the timeline as well (about half
    # the window at 150%, through six layers), and a resize step all of that under the clouds and charts too.
    db = str(tmp_path / "big.db")
    fill(db)
    screen = tmp_path / "screen.json"  # 150% as Windows sets it
    screen.write_text('{"screens": [{"name": "s", "x": 0, "y": 0, "width": 1920, "height": 1200, "logicalDpi": 144, '
                      '"logicalBaseDpi": 96, "dpr": 1}]}')
    run = subprocess.run([sys.executable, "-X", "faulthandler", "-c", CARDS_AT_150, db, str(tmp_path / "cfg150")],
                         capture_output=True, text=True, timeout=240, cwd=tmp_path,
                         env=dict(os.environ, QT_QPA_PLATFORM=f"offscreen:configfile={screen.name}"))
    assert run.returncode == 0, run.stderr[-2000:]
    out = dict(line.split(" ", 1) for line in run.stdout.splitlines() if " " in line)
    assert out["dpr"] == "1.5", run.stdout
    assert out["painted"] == "TimelineChart", run.stdout
    assert out["unpainted"] == "0", run.stdout  # no stray pixels where an edge falls between device pixels
    assert int(out["worst"]) <= 1, run.stdout  # the same pixels as the card under them, but for a gradient's rounding
