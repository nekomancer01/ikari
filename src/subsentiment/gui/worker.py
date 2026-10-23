"""Runs work without holding up the window: fetch jobs and archive counts on threads, which mostly wait on
the network, numpy work on a thread (`on_thread`), and long Python work on the GUI thread a slice at a time
(`Slices`)."""
from __future__ import annotations

import gc
import multiprocessing
import os
import sqlite3
import threading
import time
from concurrent import futures
from concurrent.futures import Future, ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from contextlib import contextmanager
from typing import Callable, Iterator

import requests
from PySide6.QtCore import QCoreApplication, QObject, QThread, QTimer, Signal, Slot

from subsentiment import collect
from subsentiment.arctic_shift import ArcticShiftClient, RateLimiter
from subsentiment.reddit import FetchError, normalize_subreddit
from subsentiment.sentiment import Lexicon
from subsentiment.store import Store

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
    WAIT = object()  # yielded by a step waiting on other work (`on_thread`): the rest of this frame is let go

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
        for item in self._steps:
            if item is self.WAIT:
                self._timer.setInterval(4)  # looked at again in a few milliseconds, not spun on
                return
            self._timer.setInterval(0)
            if time.perf_counter() >= end:
                return
        done = self._done
        self.stop()
        done()


def finish(steps: Iterator):
    """Go through ``steps`` (as Slices would) here and now, waiting out any `on_thread` work; returns what the
    generator returns."""
    while True:
        try:
            if next(steps) is Slices.WAIT:
                time.sleep(0.001)  # (lets the thread have the interpreter lock)
        except StopIteration as done:
            return done.value


def on_thread(work: Callable):
    """Run ``work`` on a thread of its own, yielding Slices.WAIT until it's done; what it returns is this
    generator's return value (an error it raised is raised here). For numpy work, which lets go of the interpreter
    lock as it goes: sorting 650,000 numbers there took 100 ms, and left every frame on time."""
    box: list = []

    def run():
        try:
            box.append((work(), None))
        except Exception as e:  # handed to the window's thread
            box.append((None, e))
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    yield Slices.WAIT  # at least once, however quick it is: work handed to a thread always lets a frame go by
    while not box:
        yield Slices.WAIT
    result, error = box[0]
    if error is not None:
        raise error
    return result


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


class ThreadScorer:
    """Scores a fetch's comments, a thread at a time, in other processes, while the fetch's own thread waits without
    the interpreter lock: scored on that thread, a big thread's comments held it for most of each second, and every
    frame of the window waited on it. The processes are the app's, shared by its fetches: the first thread big enough
    starts them, on a thread of their own, and they stay up until the app closes (``close``); each takes seconds to
    start on Windows. Until they're all up, a thread's comments are scored here, so no thread ever waits on them;
    a thread of a few comments always is (handing them over costs more), and every one once the processes can't
    run, fail or don't answer in time. Stopped while it waits, it gives back None and the thread isn't saved."""

    LEAST = 100  # comments in a thread for it to go to the processes
    WORKERS = max(1, min(4, (os.cpu_count() or 2) - 1))
    WAIT = 30.0  # seconds the processes get to answer, and a millisecond more for each comment
    _pool: ProcessPoolExecutor | None = None
    _up: list[Future] = []  # one no-op each, done once the processes have started
    _lock = threading.Lock()
    broken = False

    def __init__(self, should_stop=lambda: False):
        self.should_stop = should_stop

    @classmethod
    def start(cls) -> None:
        with cls._lock:
            if cls._pool is not None or cls.broken:
                return
            try:
                cls._pool = ProcessPoolExecutor(cls.WORKERS, mp_context=multiprocessing.get_context("spawn"))
                cls._up = [cls._pool.submit(int) for _ in range(cls.WORKERS)]
            except Exception:  # none allowed here
                cls.broken = True
        if cls.broken:
            cls.close()

    @classmethod
    def ready(cls) -> bool:
        up = cls._up
        if cls.broken or not up or not all(f.done() for f in up):
            return False
        if any(f.cancelled() or f.exception() for f in up):
            cls.broken = True
            cls.close()
            return False
        return True

    def __call__(self, lexicon: Lexicon, texts: list[str]) -> list[tuple[float, str]] | None:
        if len(texts) >= self.LEAST and not self.ready():
            if self._pool is None and not self.broken:
                threading.Thread(target=self.start, name="ThreadScorer start", daemon=True).start()
        elif len(texts) >= self.LEAST:
            cls = type(self)
            try:
                step = -(-len(texts) // self.WORKERS)
                parts = [cls._pool.submit(Lexicon.compare, lexicon.weights, frozenset(), texts[a:a + step])
                         for a in range(0, len(texts), step)]
                give_up = time.monotonic() + self.WAIT + len(texts) / 1000
                while futures.wait(parts, timeout=0.1).not_done:
                    if self.should_stop():
                        for part in parts:
                            part.cancel()
                        return None
                    if time.monotonic() > give_up:
                        raise TimeoutError("the scoring processes didn't answer")
                return [got[:2] for part in parts for got in part.result()]
            except Exception:  # they died, hung or were closed: scored here from now on
                cls.broken = True
                cls.close()
        return [lexicon.score(t) for t in texts]

    @classmethod
    def close(cls) -> None:
        """End the processes (the app is closing): idle ones would keep it from exiting."""
        with cls._lock:
            pool, cls._pool, cls._up = cls._pool, None, []
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
            for process in list((pool._processes or {}).values()):
                process.terminate()


class Worker(QObject):
    """A job for BackgroundJob, run on its thread: ``work`` does it and emits ``finished`` (each worker's own
    signal, with what it found); an error it raises is sent as ``failed``, with what to tell the user. ``stop``
    asks it to end early: ``stopping`` turns true."""
    failed = Signal(str)

    def __init__(self):
        super().__init__()
        self.stopping = False

    def stop(self) -> None:
        self.stopping = True

    @Slot()
    def run(self) -> None:
        try:
            self.work()
        except Exception as e:  # keep the GUI alive on unexpected failures too
            self.failed.emit(failure_text(e))

    def work(self) -> None:
        raise NotImplementedError


class FetchWorker(Worker):
    """Runs one fetch with a connection of its own. After each commit it reads back what it saved (since ``since``)
    and sends those rows to the window: read there, each row's read let go of the interpreter lock, and with this
    thread busy scoring, the window waited up to a switch interval for it back, row after row (seconds of frozen
    window for a big thread's comments)."""
    progress = Signal(int, int, str)
    finished = Signal(object)  # collect.FetchResult
    # Rows committed, read back: {kind: (saved at or after, [store.Row in the order saved])}, kind posts or comments.
    saved = Signal(object)

    def __init__(self, request: collect.FetchRequest, db_path: str):
        super().__init__()
        self.request = request
        self.db_path = db_path
        self.since = time.time() - 1.0  # rows saved from now on are this fetch's
        self._read_to = {"posts": self.since, "comments": self.since}
        self._last: dict[str, str] = {}  # the id of the last row of each kind sent
        self._closing = threading.Event()  # the window is closing: what's left of packing waits for the next fetch

    def _send_saved(self, store: Store) -> None:
        got = {}
        for kind in ("posts", "comments"):
            since, last = self._read_to[kind], self._last.get(kind)
            try:
                rows = store.rows(kind, normalize_subreddit(self.request.subreddit), since, words=True)
            except sqlite3.Error:  # the window reads them itself instead; the fetch goes on
                self.saved.emit({})
                return
            if rows and rows[0]["fetched_at"] == since and rows[0]["id"] == last:
                rows = rows[1:]  # the last one sent, read again (saved at or after it)
            if rows:
                self._read_to[kind], self._last[kind] = rows[-1]["fetched_at"], rows[-1]["id"]
            got[kind] = (since, rows)
        self.saved.emit(got)

    def stop(self, closing: bool = False) -> None:
        super().stop()
        if closing:
            self._closing.set()

    def work(self) -> None:
        store = None
        scorer = ThreadScorer(lambda: self.stopping)
        try:
            store = Store(self.db_path)  # sqlite connections can't cross threads; opening can fail too
            result = collect.run(ArcticShiftClient(limiter=LIMITER), store, self.request,
                                 progress=self.progress.emit, should_stop=lambda: self.stopping,
                                 saved=lambda: self._send_saved(store), score_all=scorer)
            self._send_saved(store)  # whatever its last commits saved without a word
            self.finished.emit(result)
        finally:
            LIMITER.on_wait = None  # collect.run pointed it at this worker, which is about to go
            if store is not None:
                try:
                    store.pack_settled(stop=self._closing)  # days that have settled since get their text compressed
                except Exception:  # never worth failing a fetch for: the rows stay as they are, packed next time
                    pass
                store.close()


class Collector(QObject):
    """Python's garbage collector, run on the window's thread only. Left to itself it collects whenever enough
    objects have been made, on whichever thread made them: a table reading rows on a thread then deleted the
    widgets of a window closed earlier, there, and Qt crashed (a segfault in Linux CI). So the automatic one is
    off, and every TICK this collects here instead.
    `start` once a QApplication is up; `lasting` holds it off."""

    TICK = 100  # ms
    _one: Collector | None = None
    _held = 0  # `lasting` reads going on
    _lock = threading.Lock()

    def __init__(self):
        super().__init__(QCoreApplication.instance())
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(self.TICK)
        self._timer.timeout.connect(self.collect)
        self._timer.start()

    @classmethod
    def start(cls) -> None:
        if cls._one is None:
            cls._one = cls()

    def collect(self) -> None:
        if Collector._held:
            return
        # The youngest generation every time (the objects made since, however many were let go meanwhile), and an
        # older one, with those younger, once that many collections of the one below it have gone by.
        counts, limits = gc.get_count(), gc.get_threshold()
        gc.collect(2 if counts[2] >= limits[2] else 1 if counts[1] >= limits[1] else 0)

    @classmethod
    @contextmanager
    def lasting(cls, freeze: bool = True):
        """Read rows that stay for as long as their subreddit is open: the garbage collector went through every
        one of them again and again, a third of a second each time with 650,000 comments, while they were read and
        after, holding up the window's frames. They never make a cycle, so they're read with it held off, then
        left out of its passes (gc.freeze); their refcounts still let them go. Without ``freeze`` it's only held
        off: freezing keeps whatever is alive then out of every later pass, so what it holds that goes in a cycle
        (a subreddit's sqlite connections once let go, a closed window's widgets) would never be freed."""
        collecting = gc.isenabled()
        gc.disable()
        with cls._lock:
            cls._held += 1
        try:
            yield
        finally:
            if freeze:
                gc.freeze()
            with cls._lock:
                cls._held -= 1
            if collecting:
                gc.enable()


class LoadWorker(Worker):
    """Reads a saved subreddit with connections of its own, so opening a big one doesn't hold up the window: numpy,
    zstd and sqlite let go of the interpreter lock as they work, so the window paints meanwhile. ``read`` sends its
    posts and comments as rows.Saved (a RowIndex each, 0.7 s for 650,000 comments); with ``rows``, every one of
    them as a store.Row instead (half a minute for those), for Rescore to search through."""
    read = Signal(object)  # (tag, {kind: rows.Saved, or a list of rows}): the tag it was given
    finished = Signal(object)  # (tag,)

    def __init__(self, db_path: str, subreddit: str, tag=None, rows: bool = False, words: bool = True):
        super().__init__()
        self.db_path, self.subreddit, self.tag, self.rows, self.words = db_path, subreddit, tag, rows, words
        self.store: Store | None = None

    def stop(self) -> None:
        """Cut the read short (the window is closing): Qt aborted the process when the window went with its thread
        still running."""
        super().stop()
        store = self.store
        if store is not None:
            store.db.interrupt()  # (from any thread) the query being read fails at its next row

    def work(self) -> None:
        from subsentiment.gui.rows import Saved  # (it runs work on threads with on_thread, from here)
        store = None
        try:
            if self.rows:
                store = self.store = Store(self.db_path)
                if self.stopping:
                    raise sqlite3.OperationalError("interrupted")
                with store.snapshot(), Collector.lasting():
                    got = {k: store.rows(k, self.subreddit) for k in ("posts", "comments")}
            else:
                got = {}
                with Collector.lasting(freeze=False):
                    for kind in ("posts", "comments"):
                        if self.stopping:
                            raise sqlite3.OperationalError("interrupted")
                        got[kind] = Saved(self.db_path, kind, self.subreddit, self.words)
            self.read.emit((self.tag, got))
            self.finished.emit((self.tag,))
        finally:
            if store is not None:
                self.store = None
                store.close()


class StoreWorker(Worker):
    """Makes one change to the saved data with a connection of its own, such as deleting days and giving their
    space back: sqlite lets go of the interpreter while it works, so the window paints meanwhile. Stop doesn't cut
    it short: it's one transaction, which ends by itself."""
    finished = Signal(object)  # what the Store method returned

    def __init__(self, db_path: str, method: str, *args):
        super().__init__()
        self.db_path, self.method, self.args = db_path, method, args

    def work(self) -> None:
        store = Store(self.db_path)
        try:
            self.finished.emit(getattr(store, self.method)(*self.args))
        finally:
            store.close()


class CountWorker(Worker):
    """Asks the archive how many posts each day in a range has (ArcticShiftClient.day_counts)."""
    finished = Signal(str, object)  # subreddit, {day start: count}

    def __init__(self, subreddit: str, after: float, before: float):
        super().__init__()
        self.args = (subreddit, after, before)

    def work(self) -> None:
        stopping = lambda: self.stopping  # noqa: E731
        counts = ArcticShiftClient(limiter=LIMITER, should_stop=stopping).day_counts(*self.args, should_stop=stopping)
        self.finished.emit(self.args[0], counts)


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


class StoreRead(QObject):
    """Reads saved data with a Store method on a thread of its own (a StoreWorker), so the window paints meanwhile.
    ``got`` emits (what was asked, the answer) for each read. Asked again while a read runs, it reads once more
    when that one ends, with the newest ask; the answer in hand is still given, so asks coming faster than reads
    never hold every answer back. A failed read gives nothing."""
    got = Signal(object, object)

    def __init__(self, db_path: str, method: str, parent: QObject | None = None):
        super().__init__(parent)
        self.db_path, self.method = db_path, method
        self.job = BackgroundJob(self)
        self.job.ended.connect(self._ended)
        self._asked: tuple | None = None
        self._next: tuple | None = None
        self._answer = None

    @property
    def busy(self) -> bool:
        return self.job.busy

    def ask(self, *args) -> None:
        self._next = args
        if not self.job.busy:
            self._start()

    def close(self) -> None:
        """As the app closes: a read under way is waited for, and its answer dropped."""
        self._next = None
        if self.job.busy:
            self.job.ended.disconnect()
            self.job.qthread.quit()
            self.job.qthread.wait(3000)

    def _start(self) -> None:
        self._asked, self._next, self._answer = self._next, None, None
        worker = StoreWorker(self.db_path, self.method, *self._asked)
        worker.finished.connect(self._on_answer)  # a bound method, so it's handled on this object's thread
        self.job.start(worker)

    @Slot(object)
    def _on_answer(self, answer) -> None:
        self._answer = answer

    @Slot()
    def _ended(self) -> None:
        asked, answer = self._asked, self._answer
        if self._next is not None:  # asked again meanwhile
            self._start()
        if answer is not None:
            self.got.emit(asked, answer)
