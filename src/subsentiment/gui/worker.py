"""Runs fetch jobs and archive counts off the GUI thread."""
from __future__ import annotations

import requests
from PySide6.QtCore import QObject, Signal, Slot

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

