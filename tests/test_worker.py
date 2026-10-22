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


def test_a_big_threads_comments_are_scored_in_other_processes_as_they_are_here():
    from subsentiment.sentiment import Lexicon
    lexicon = Lexicon({"raid": 0.8, "nerf": -0.6})
    moods = ["was great", "is so annoying", "needs a nerf", "ok I guess", "what a raid", "not bad at all"]
    texts = [f"the raid tonight {moods[i % len(moods)]} #{i}" for i in range(worker_mod.ThreadScorer.LEAST + 50)]
    scorer = worker_mod.ThreadScorer()
    try:
        assert scorer(lexicon, texts[:5]) == [lexicon.score(t) for t in texts[:5]] and scorer._pool is None  # here
        got = scorer(lexicon, texts)
        assert scorer._pool is not None and not scorer.broken
    finally:
        scorer.close()
    assert got == [lexicon.score(t) for t in texts]


def test_stop_while_a_threads_comments_are_scored_elsewhere_leaves_the_thread_unsaved():
    # Waiting on the processes, Stop (or quitting) used to wait for the whole thread's scores first.
    from subsentiment.sentiment import Lexicon
    texts = ["the raid tonight was great"] * (worker_mod.ThreadScorer.LEAST * 50)
    scorer = worker_mod.ThreadScorer(lambda: True)
    start = time.monotonic()
    try:
        assert scorer(Lexicon({"raid": 0.8}), texts) is None
    finally:
        scorer.close()
    assert time.monotonic() - start < 10 and scorer._pool is None and not scorer.broken

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
