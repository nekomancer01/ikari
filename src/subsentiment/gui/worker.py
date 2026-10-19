"""Runs fetch jobs and archive counts off the GUI thread."""
from __future__ import annotations

import threading

import requests
from PySide6.QtCore import QObject, Signal, Slot

from subsentiment import collect
from subsentiment.arctic_shift import ArcticShiftClient, RateLimiter
from subsentiment.reddit import FetchError
from subsentiment.store import Store

# One limiter for every request the app sends, so a count running beside a fetch doesn't double the rate.
LIMITER = RateLimiter()


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
        store = Store(self.db_path)  # sqlite connections can't cross threads
        try:
            result = collect.run(ArcticShiftClient(limiter=LIMITER), store, self.request,
                                 progress=self.progress.emit, should_stop=lambda: self._stop,
                                 saved=self.saved.emit)
            self.finished.emit(result)
        except FetchError as e:
            self.failed.emit(str(e))
        except requests.RequestException as e:
            self.failed.emit(f"Network error: {e}")
        except Exception as e:  # keep the GUI alive on unexpected failures
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            LIMITER.on_wait = None  # collect.run pointed it at this worker, which is about to go
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
            counts = ArcticShiftClient(limiter=LIMITER).day_counts(*self.args, should_stop=lambda: self._stop)
            self.finished.emit(self.args[0], counts)
        except FetchError as e:
            self.failed.emit(str(e))
        except requests.RequestException as e:
            self.failed.emit(f"Network error: {e}")
        except Exception as e:
            self.failed.emit(f"{type(e).__name__}: {e}")


class LiveWorker(QObject):
    """Polls for new posts of one subreddit every ``interval`` seconds until stopped."""
    status = Signal(str)
    saved = Signal(int, object)  # posts saved by a poll that found some, newest created time
    polled = Signal(object)  # collect.LiveResult, after every poll
    failed = Signal(str)
    finished = Signal()

    def __init__(self, subreddit: str, db_path: str, interval: float = 30.0):
        super().__init__()
        self.subreddit = subreddit
        self.db_path = db_path
        self.interval = interval
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    @Slot()
    def run(self) -> None:
        store = Store(self.db_path)
        client = ArcticShiftClient()
        client.limiter.on_wait = lambda secs, why: self.status.emit(f"{why}. Waiting {secs:.0f} s…")
        try:
            while not self._stop.is_set():
                try:
                    result = collect.poll_live(client, store, self.subreddit)
                    self.polled.emit(result)
                    if result.new:
                        self.saved.emit(result.new, result.newest)
                except (FetchError, requests.RequestException) as e:
                    self.status.emit(f"Live feed couldn't reach Arctic Shift ({e}). Trying again shortly.")
                self._stop.wait(self.interval)
        except Exception as e:  # keep the GUI alive on unexpected failures
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            store.close()
            self.finished.emit()
