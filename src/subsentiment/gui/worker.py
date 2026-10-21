"""Runs work without holding up the window: fetch jobs and archive counts on threads, which mostly wait on
the network, and long Python work on the GUI thread a slice at a time (`Slices`)."""
from __future__ import annotations

import gc
import multiprocessing
import os
import sqlite3
import time
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from typing import Callable, Iterator

import requests
from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from subsentiment import collect
from subsentiment.arctic_shift import ArcticShiftClient, RateLimiter
from subsentiment.reddit import FetchError
from subsentiment.sentiment import Lexicon
from subsentiment.analysis import Analysis
from subsentiment.gui.days import SavedRows
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


class ScorePool(QObject):
    """Scores texts by a subreddit's words in other processes, one per spare core: long scoring then neither
    holds up the window (a thread would, by the interpreter lock) nor waits for room between its frames. The
    processes start at the first ``submit`` and end a minute after ``rest`` (so setting words one after another
    doesn't start them each time), or at ``close``. ``scored`` brings (tag, [(score, label, label without the
    words)]) back on the GUI thread, or ``failed`` the tag of texts they couldn't score. Once processes can't
    run here, every later submit fails at once rather than starting more."""

    scored = Signal(object, object)
    failed = Signal(object)
    _done = Signal(object, object, object)  # from the pool's own thread: the pool, tag, future

    WORKERS = max(1, min(8, (os.cpu_count() or 2) - 1))
    IDLE_MS = 60_000

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._pool: ProcessPoolExecutor | None = None
        self.broken = False
        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(self.IDLE_MS)
        self._idle.timeout.connect(self.close)
        self._done.connect(self._settle)

    def submit(self, tag, weights: dict[str, float], words: frozenset[str], texts: list[str]) -> None:
        self._idle.stop()
        try:
            if self.broken:
                raise BrokenProcessPool("worker processes can't run here")
            if self._pool is None:
                self._pool = ProcessPoolExecutor(self.WORKERS, mp_context=multiprocessing.get_context("spawn"))
            pool = self._pool
            future = pool.submit(Lexicon.compare, weights, words, texts)
        except Exception:  # none allowed, or they died: the caller scores them itself
            self.broken = True
            self.close()
            QTimer.singleShot(0, lambda: self.failed.emit(tag))
            return
        future.add_done_callback(lambda f: self._send(pool, tag, f))

    def rest(self) -> None:
        if self._pool is not None:
            self._idle.start()

    def close(self) -> None:
        """End the processes now, and what they're on with them (it's for work dropped, or the app is closing)."""
        pool, self._pool = self._pool, None
        self._idle.stop()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
            for process in list((pool._processes or {}).values()):
                process.terminate()

    def _send(self, pool, tag, future: Future) -> None:
        try:
            self._done.emit(pool, tag, future)
        except RuntimeError:  # the window has closed
            pass

    def _settle(self, pool, tag, future: Future) -> None:
        try:
            results = future.result()
        except BrokenProcessPool:  # its processes died: ended by close, or they can't run here
            if pool is self._pool:
                self.broken = True
                self.close()
            self.failed.emit(tag)
        except Exception:  # cancelled by close, or this chunk alone failed
            self.failed.emit(tag)
        else:
            self.scored.emit(tag, results)


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


@contextmanager
def lasting():
    """Read rows that stay for as long as their subreddit is open: the garbage collector went through every one of
    them again and again, a third of a second each time with 650,000 comments, while they were read and after,
    holding up the window's frames. They never make a cycle, so they're read with it off, then left out of its
    passes (gc.freeze); their refcounts still let them go."""
    collecting = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if collecting:
            gc.freeze()
            gc.enable()


class LoadWorker(QObject):
    """Reads a saved subreddit with a connection of its own and counts it up, so opening a big one doesn't
    hold up the window: sqlite lets go of the interpreter while it reads, so the window paints meanwhile."""
    finished = Signal(object)  # (tag, rows, analysis): the tag it was given, {kind: rows}, {kind: Analysis}
    failed = Signal(str)

    def __init__(self, db_path: str, subreddit: str, min_scores: tuple, tag=None):
        super().__init__()
        self.args = (db_path, subreddit, min_scores)
        self.tag = tag
        self.store: Store | None = None
        self._stop = False

    def stop(self) -> None:
        """Cut the read short (the window is closing): reading a big subreddit takes half a minute, and Qt aborted
        the process when the window went with its thread still running."""
        self._stop = True
        store = self.store
        if store is not None:
            store.db.interrupt()  # (from any thread) the query being read fails at its next row

    @Slot()
    def run(self) -> None:
        db_path, subreddit, (min_post, min_comment) = self.args
        store = None
        try:
            store = self.store = Store(db_path)
            if self._stop:
                raise sqlite3.OperationalError("interrupted")
            with store.snapshot(), lasting():
                rows = {k: SavedRows(scoring_at_least(store.rows(k, subreddit, words=True), least))
                        for k, least in (("posts", min_post), ("comments", min_comment))}
            # Counted newest first, as Store.posts reads them: a total's last digits depend on the order it's added up in.
            self.finished.emit((self.tag, rows, {k: Analysis(reversed(v)) for k, v in rows.items()}))
        except Exception as e:
            self.failed.emit(failure_text(e))
        finally:
            if store is not None:
                self.store = None
                store.close()


class StoreWorker(QObject):
    """Makes one change to the saved data with a connection of its own, such as deleting days and giving their
    space back: sqlite lets go of the interpreter while it works, so the window paints meanwhile."""
    finished = Signal(object)  # what the Store method returned
    failed = Signal(str)

    def __init__(self, db_path: str, method: str, *args):
        super().__init__()
        self.db_path, self.method, self.args = db_path, method, args

    def stop(self) -> None:
        pass  # one transaction; it ends by itself

    @Slot()
    def run(self) -> None:
        store = None
        try:
            store = Store(self.db_path)
            self.finished.emit(getattr(store, self.method)(*self.args))
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
    """Runs one worker at a time on its own QThread: the fetch, Download coverage's count or deletion, or reading
    a saved subreddit.

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
