"""Runs a fetch job off the GUI thread."""
from __future__ import annotations

import requests
from PySide6.QtCore import QObject, Signal, Slot

from subsentiment import collect
from subsentiment.reddit import RedditError
from subsentiment.sources import make_client
from subsentiment.store import Store


class FetchWorker(QObject):
    progress = Signal(int, int, str)
    finished = Signal(object)  # collect.FetchResult
    failed = Signal(str)

    def __init__(self, credentials: dict, request: collect.FetchRequest, db_path: str):
        super().__init__()
        self.credentials = credentials
        self.request = request
        self.db_path = db_path
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @Slot()
    def run(self) -> None:
        store = Store(self.db_path)  # sqlite connections can't cross threads
        try:
            client = make_client(self.credentials)
            result = collect.run(client, store, self.request,
                                 progress=self.progress.emit, should_stop=lambda: self._stop)
            self.finished.emit(result)
        except RedditError as e:
            self.failed.emit(str(e))
        except requests.RequestException as e:
            self.failed.emit(f"Network error: {e}")
        except Exception as e:  # keep the GUI alive on unexpected failures
            self.failed.emit(f"{type(e).__name__}: {e}")
        finally:
            store.close()
