"""Runs fetch jobs and archive counts off the GUI thread."""
from __future__ import annotations

import requests
from PySide6.QtCore import QObject, QThread, Signal, Slot

from subsentiment import collect
from subsentiment.arctic_shift import ArcticShiftClient, RateLimiter
from subsentiment.reddit import FetchError
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
    """Runs one worker at a time on its own QThread: the fetch, or Download coverage's count.

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
