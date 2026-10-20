"""Runs work without holding up the window: fetch jobs and archive counts on threads, which mostly wait on
the network, and long Python work on the GUI thread a slice at a time (`Slices`)."""
from __future__ import annotations

import time
from typing import Callable, Iterator

import requests
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from subsentiment import collect
from subsentiment.arctic_shift import ArcticShiftClient, RateLimiter
from subsentiment.reddit import FetchError
from subsentiment.analysis import Analysis
from subsentiment.store import Store, scoring_at_least

# One limiter for every request the app sends, so a count running beside a fetch doesn't double the rate.
LIMITER = RateLimiter()


def failure_text(e: Exception) -> str:
    """What to tell the user about a job that raised ``e``."""
    if isinstance(e, FetchError):
        return str(e)
    if isinstance(e, requests.RequestException):
        return f"Network error: {e}"
    return f"{type(e).__name__}: {e}"


class Slices(QObject):
    """Steps through long work on the GUI thread a few milliseconds at a time, painting and answering the mouse
    between slices. Python on another thread would hold the interpreter lock the GUI thread needs for every
    call into Qt, so a busy thread made every frame wait on it; this way frames go on at full rate.

    ``start(steps, done)``: each item ``steps`` yields is one slice of the work; ``done`` is called once there
    are no more. Starting again, or ``stop``, drops the work underway."""

    BUDGET = 0.006  # seconds of work between two frames

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._timer = QTimer(self)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._step)
        self._steps: Iterator | None = None
        self._done: Callable[[], None] | None = None

    def start(self, steps: Iterator, done: Callable[[], None]) -> None:
        self._steps, self._done = iter(steps), done
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._steps = self._done = None

    def running(self) -> bool:
        return self._timer.isActive()

    def _step(self) -> None:
        end = time.perf_counter() + self.BUDGET
        for _ in self._steps:
            if time.perf_counter() >= end:
                return
        done = self._done
        self.stop()
        done()


class FetchWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)  # collect.FetchResult
    failed = Signal(str)
    saved = Signal()  # some rows were committed; the window can show them

    def __init__(self, request: collect.FetchRequest, db_path: str):
        super().__init__()
        self.request = request
        self.db_path = db_path
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @Slot()
    def run(self) -> None:
        store = None
        try:
            store = Store(self.db_path)  # sqlite connections can't cross threads; opening can fail too
            result = collect.run(ArcticShiftClient(limiter=LIMITER), store, self.request,
                                 progress=self.progress.emit, should_stop=lambda: self._stop,
                                 saved=self.saved.emit)
            self.finished.emit(result)
        except Exception as e:  # keep the GUI alive on unexpected failures too
            self.failed.emit(failure_text(e))
        finally:
            LIMITER.on_wait = None  # collect.run pointed it at this worker, which is about to go
            if store is not None:
                try:
                    store.pack_settled()  # days that have settled since get their text compressed (store.PACKED)
                except Exception:  # never worth failing a fetch for: the rows stay as they are, packed next time
                    pass
                store.close()


class LoadWorker(QObject):
    """Reads a saved subreddit with a connection of its own and counts it up, so opening a big one doesn't
    hold up the window: sqlite lets go of the interpreter while it reads, so the window paints meanwhile."""
    finished = Signal(object)  # (tag, rows, analysis): the tag it was given, {kind: rows}, {kind: Analysis}
    failed = Signal(str)

    def __init__(self, db_path: str, subreddit: str, min_score: int | None, tag=None):
        super().__init__()
        self.args = (db_path, subreddit, min_score)
        self.tag = tag

    def stop(self) -> None:
        pass  # one read; the window ignores what it sends once closed

    @Slot()
    def run(self) -> None:
        db_path, subreddit, min_score = self.args
        store = None
        try:
            store = Store(db_path)
            with store.snapshot():
                rows = {"posts": scoring_at_least(store.posts(subreddit), min_score),
                        "comments": scoring_at_least(store.comments(subreddit), min_score)}
            self.finished.emit((self.tag, rows, {k: Analysis(v) for k, v in rows.items()}))
        except Exception as e:
            self.failed.emit(failure_text(e))
        finally:
            if store is not None:
                store.close()


class CountWorker(QObject):
    """Asks the archive how many posts each day in a range has (ArcticShiftClient.day_counts)."""
    finished = Signal(str, object)  # subreddit, {day start: count}
    failed = Signal(str)

    def __init__(self, subreddit: str, after: float, before: float):
        super().__init__()
        self.args = (subreddit, after, before)
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @Slot()
    def run(self) -> None:
        try:
            counts = ArcticShiftClient(limiter=LIMITER, should_stop=lambda: self._stop).day_counts(
                *self.args, should_stop=lambda: self._stop)
            self.finished.emit(self.args[0], counts)
        except Exception as e:
            self.failed.emit(failure_text(e))


class BackgroundJob(QObject):
    """Runs one worker at a time on its own QThread: the fetch, Download coverage's count, or reading a saved
    subreddit.

    Connect the worker's own signals, then ``start`` it. The worker's ``run`` slot does the work; its
    ``finished`` or ``failed`` signal ends the thread, and once the thread has finished the job lets go of
    both and emits ``ended``."""

    ended = Signal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.qthread: QThread | None = None
        self.worker: QObject | None = None

    @property
    def busy(self) -> bool:
        """A worker was started and its thread hasn't been tidied up yet."""
        return self.qthread is not None

    def running(self) -> bool:
        return self.qthread is not None and self.qthread.isRunning()

    def start(self, worker: QObject) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._end)
        self.qthread, self.worker = thread, worker
        thread.start()

    def stop(self) -> None:
        """Ask the worker to stop; it ends (and ``ended`` fires) once what's under way returns."""
        if self.worker is not None:
            self.worker.stop()

    def _end(self) -> None:
        self.qthread.deleteLater()
        self.worker.deleteLater()
        self.qthread = self.worker = None
        self.ended.emit()
