"""The fetch worker's error reporting and cleanup, with the Reddit client replaced by a fake."""
import os
import time

import pytest
import requests

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from subsentiment import collect  # noqa: E402
from subsentiment.gui import worker as worker_mod  # noqa: E402
from subsentiment.reddit import Comment, FetchError, Post  # noqa: E402


def post(i):
    return Post(f"p{i}", "Python", f"title {i}", "great", "a", 1, 0.9, 2, 1_700_000_000.0, "https://r", "")


class FakeClient:
    posts = [post(1), post(2)]
    error = None

    def __init__(self, **credentials):
        self.credentials = credentials

    def fetch_posts(self, *a, **kw):
        if self.error:
            raise self.error
        return list(self.posts)

    def fetch_comments(self, p, limit, pick="top"):
        return [Comment(f"c{p.id}", p.id, "Python", "", "b", "lovely", 1, 1.0, 0, "u")]


@pytest.fixture
def run(monkeypatch, tmp_path):
    def _run(error=None, include_comments=False, stop=False):
        FakeClient.error = error
        monkeypatch.setattr(worker_mod, "ArcticShiftClient", FakeClient)
        w = worker_mod.FetchWorker(collect.FetchRequest("python", include_comments=include_comments),
                                   str(tmp_path / "w.db"))
        events = {"finished": [], "failed": [], "progress": []}
        w.finished.connect(events["finished"].append)
        w.failed.connect(events["failed"].append)
        w.progress.connect(lambda *a: events["progress"].append(a))
        if stop:
            w.stop()
        w.run()
        return events
    return _run


def test_success_emits_result_and_stores_rows(run, tmp_path):
    ev = run(include_comments=True)
    assert ev["failed"] == []
    (result,) = ev["finished"]
    assert (result.posts, result.comments, result.stopped) == (2, 2, False)
    from subsentiment.store import Store
    s = Store(tmp_path / "w.db")
    assert len(s.posts("python")) == 2 and len(s.comments("python")) == 2


def test_stop_before_comments_reports_stopped(run):
    (result,) = run(include_comments=True, stop=True)["finished"]
    assert result.stopped is True and result.comments == 0


@pytest.mark.parametrize("error, text", [
    (FetchError("Access denied (HTTP 403)"), "Access denied (HTTP 403)"),
    (requests.ConnectionError("dns"), "Network error: dns"),
    (ValueError("boom"), "ValueError: boom"),
])
def test_errors_become_failed_signals_not_exceptions(run, error, text):
    ev = run(error=error)
    assert ev["failed"] == [text] and ev["finished"] == []


def test_connection_is_closed_even_on_failure(run, monkeypatch):
    closed = []
    real_close = worker_mod.Store.close
    monkeypatch.setattr(worker_mod.Store, "close", lambda self: (closed.append(1), real_close(self)))
    run(error=FetchError("x"))
    assert closed == [1]


def test_a_database_that_cant_be_opened_fails_the_fetch_instead_of_hanging_it(tmp_path):
    # Opening the store sat outside the try: neither finished nor failed was sent, the thread never quit
    # and the window stayed "fetching" with Stop doing nothing.
    blocker = tmp_path / "not-a-folder"
    blocker.write_text("")
    w = worker_mod.FetchWorker(collect.FetchRequest("python"), str(blocker / "w.db"))
    events = {"finished": [], "failed": []}
    w.finished.connect(events["finished"].append)
    w.failed.connect(events["failed"].append)
    w.run()
    assert events["finished"] == [] and len(events["failed"]) == 1


def test_a_background_job_runs_its_worker_off_the_gui_thread_and_tidies_up():
    import threading
    import time
    from PySide6.QtCore import QObject, Signal, Slot
    from PySide6.QtWidgets import QApplication

    # A QApplication, not a QCoreApplication: the first app made in a process is the one every later test
    # gets, and a window built on a QCoreApplication aborts the whole test run.
    app = QApplication.instance() or QApplication([])

    class Worker(QObject):
        finished = Signal(object)
        failed = Signal(str)
        ran_on = None

        @Slot()
        def run(self):
            Worker.ran_on = threading.current_thread()
            self.finished.emit(None)

        def stop(self):
            pass

    job = worker_mod.BackgroundJob()
    ended = []
    job.ended.connect(lambda: ended.append(True))
    job.start(Worker())
    assert job.busy
    end = time.monotonic() + 5
    while not ended and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert ended and not job.busy and not job.running()
    assert Worker.ran_on is not threading.main_thread()


@pytest.fixture
def scorers(monkeypatch):
    """The app's scoring processes, ended after the test (they're shared by its fetches until the app closes)."""
    monkeypatch.setattr(worker_mod.ThreadScorer, "broken", False)
    yield worker_mod.ThreadScorer
    worker_mod.ThreadScorer.close()


def up(scorers) -> None:
    end = time.monotonic() + 60
    while not scorers.ready():
        assert time.monotonic() < end and not scorers.broken
        time.sleep(0.01)


def test_a_big_threads_comments_are_scored_in_other_processes_as_they_are_here(scorers):
    from subsentiment.sentiment import Lexicon
    lexicon = Lexicon({"raid": 0.8, "nerf": -0.6})
    moods = ["was great", "is so annoying", "needs a nerf", "ok I guess", "what a raid", "not bad at all"]
    texts = [f"the raid tonight {moods[i % len(moods)]} #{i}" for i in range(scorers.LEAST + 50)]
    scorers.start()
    up(scorers)
    scorer = scorers()
    handed = count_calls_on(scorers._pool, "submit")
    assert scorer(lexicon, texts[:5]) == [lexicon.score(t) for t in texts[:5]] and not handed  # here
    assert scorer(lexicon, texts) == [lexicon.score(t) for t in texts]
    assert handed and not scorers.broken


def test_a_big_thread_is_scored_here_while_the_processes_start_and_the_next_ones_go_to_them(scorers):
    # Waiting on them, each thread of a download waited seconds on Windows, every fetch (they were started for each).
    from subsentiment.sentiment import Lexicon
    lexicon = Lexicon({"raid": 0.8})
    texts = [f"the raid tonight #{i}" for i in range(scorers.LEAST)]
    scorer = scorers()
    assert scorers._pool is None
    assert scorer(lexicon, texts) == [lexicon.score(t) for t in texts]  # here, starting them meanwhile
    up(scorers)
    handed = count_calls_on(scorers._pool, "submit")
    assert scorers()(lexicon, texts) == [lexicon.score(t) for t in texts] and handed  # another fetch's, there


def test_stop_while_a_threads_comments_are_scored_elsewhere_leaves_the_thread_unsaved(scorers):
    # Waiting on the processes, Stop (or quitting) used to wait for the whole thread's scores first.
    from subsentiment.sentiment import Lexicon
    texts = ["the raid tonight was great"] * (scorers.LEAST * 50)
    scorers.start()
    up(scorers)
    start = time.monotonic()
    assert scorers(lambda: True)(Lexicon({"raid": 0.8}), texts) is None
    assert time.monotonic() - start < 10 and scorers.ready()  # still up for the next fetch


def test_scoring_processes_that_dont_answer_in_time_are_given_up_for_scoring_here(scorers, monkeypatch):
    # A pool that never answered would have held the fetch forever.
    from subsentiment.sentiment import Lexicon
    lexicon = Lexicon({"raid": 0.8})
    texts = [f"the raid tonight #{i}" for i in range(scorers.LEAST)]
    scorers.start()
    up(scorers)
    # Busy processes, so the thread's parts can't come back within the first 0.1 s look (on a quiet runner they
    # did, before the deadline was checked once).
    for _ in range(scorers.WORKERS):
        scorers._pool.submit(time.sleep, 2)
    monkeypatch.setattr(scorers, "WAIT", -1.0)  # out of time at once
    assert scorers()(lexicon, texts) == [lexicon.score(t) for t in texts]
    assert scorers.broken and scorers._pool is None


def count_calls_on(obj, name):
    calls, real = [], getattr(obj, name)
    setattr(obj, name, lambda *a, **k: calls.append(a) or real(*a, **k))
    return calls


def test_a_fetch_reads_back_what_it_saved_and_sends_it_to_the_window(monkeypatch, tmp_path):
    # The window used to read them itself: a read a row at a time, each waiting on this thread for the interpreter.
    monkeypatch.setattr(worker_mod, "ArcticShiftClient", FakeClient)
    FakeClient.error = None
    w = worker_mod.FetchWorker(collect.FetchRequest("python", include_comments=True), str(tmp_path / "w.db"))
    sent = []
    w.saved.connect(sent.append)
    w.run()
    posts = [r["id"] for got in sent for r in got["posts"][1]]
    comments = [r["id"] for got in sent for r in got["comments"][1]]
    assert sorted(set(posts)) == ["p1", "p2"] and sorted(set(comments)) == ["cp1", "cp2"]
    assert sent[0]["posts"][0] == w.since  # each from where the one before left off
    assert all(got[k][0] <= later[k][0] for got, later in zip(sent, sent[1:]) for k in ("posts", "comments"))
    assert all(r.words is not None for got in sent for r in got["comments"][1])  # split there, not in the window


def test_the_garbage_collectors_whole_pass_waits_for_enough_new_objects_and_for_the_mouse(monkeypatch):
    """A pass through every object took 0.15-0.2 s with a big subreddit open, on the window's thread, and ran every
    ten seconds or so (live, a year's download). It waits until the objects lasting since make a quarter as many
    again, as Python's own collector does; then, while the mouse moves or is held, up to DEFER. The younger
    generations go on every tick."""
    import types
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    asked, clock, mouse = [], [100.0], [QPoint(0, 0)]
    sizes = {0: 3000, 1: 0, 2: 1_000_000}  # objects in each generation

    def collect(generation):
        asked.append(generation)
        if generation == 1:
            sizes[2] += sizes[0] + sizes[1]
            sizes[1] = 0
        return 0
    fake_gc = types.SimpleNamespace(disable=lambda: None, collect=collect, get_count=lambda: (5, 10, 10),
                                    get_threshold=lambda: (700, 10, 10), get_objects=lambda g: [None] * sizes[g])
    monkeypatch.setattr(worker_mod, "gc", fake_gc)
    monkeypatch.setattr(worker_mod, "time", types.SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(worker_mod, "QCursor", types.SimpleNamespace(pos=lambda: mouse[0]))
    monkeypatch.setattr(worker_mod.QGuiApplication, "mouseButtons", staticmethod(lambda: Qt.MouseButton.NoButton))
    collector = worker_mod.Collector()
    collector._timer.stop()

    def tick(moving=False):
        if moving:
            mouse[0] = mouse[0] + QPoint(3, 1)
        clock[0] += 0.1
        collector.collect()
    tick()
    clock[0] += 2
    tick()
    assert asked[-1] == 2  # the first: nothing is known yet about what lasts
    asked.clear()
    for _ in range(80):  # 80 x 3,000 objects lasting: under a quarter of the million there
        tick()
    assert 2 not in asked and set(asked) == {1}
    for _ in range(20):  # past it, but the mouse is dragging across a table
        tick(moving=True)
    assert 2 not in asked
    clock[0] += 1.1  # let go of: the whole pass runs
    tick()
    assert asked[-1] == 2
    asked.clear()
    sizes[0] = 400_000  # a big lot made at once: due again at the next tick
    for _ in range(int(collector.DEFER / 0.1) + 5):  # due again, and the mouse moves on and on: it runs once DEFER
        tick(moving=True)                            # has gone by
    assert asked.count(2) == 1 and asked.index(2) >= collector.DEFER / 0.1 - 1
    collector.deleteLater()
