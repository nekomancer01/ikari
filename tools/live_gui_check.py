"""Runs the real app window (offscreen) against the live Arctic Shift API and reports what happened.

Run from the repo root:  QT_QPA_PLATFORM=offscreen python tools/live_gui_check.py [subreddit]
Does a normal fetch, a whole day's Download all and that day's Download all comments, into a throwaway
database and settings folder. Prints every HTTP status the archive sent, any 4xx/5xx, warning dialogs,
how long the window spent reading fetched rows back, and which subreddits the tables showed.
"""
from __future__ import annotations

import collections
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402
from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from subsentiment import collect  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402

statuses: collections.Counter = collections.Counter()
bad: list = []
_get = requests.Session.get


def _logged_get(self, url, **kw):
    r = _get(self, url, **kw)
    statuses[r.status_code] += 1
    if r.status_code >= 400:
        bad.append((r.status_code, url, kw.get("params"), r.text[:200]))
    return r


def main() -> None:
    sub = sys.argv[1] if len(sys.argv) > 1 else "python"
    requests.Session.get = _logged_get
    app = QApplication([])
    app.setOrganizationName("ikari-live-check")
    app.setApplicationName("ikari-live-check")
    tmp = tempfile.mkdtemp()
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tmp)
    QSettings().setValue("updates/auto", False)
    warnings: list = []
    QMessageBox.warning = lambda *a, **k: warnings.append(a[2] if len(a) > 2 else a)
    w = mw.MainWindow(str(Path(tmp) / "live.db"))
    w.show()
    spent = [0.0]
    queue = w._queue_new_rows

    def timed():
        t = time.perf_counter()
        queue()
        spent[0] += time.perf_counter() - t
    w._queue_new_rows = timed

    def run(label, start):
        t0, spent[0] = time.time(), 0.0
        start()
        while w.thread is not None:
            app.processEvents()
            time.sleep(0.005)
        for _ in range(50):
            app.processEvents()
            time.sleep(0.01)
        model = w.posts_tab.model
        subs = {model.row_at(i)["subreddit"].lower() for i in range(model.rowCount())}
        print(f"{label}: {time.time() - t0:.0f} s, {spent[0]:.1f} s reading rows back; "
              f"tables show {sorted(subs)}; {w.progress_msg.text()}")

    w.subreddit.setText(sub)
    w.limit.setValue(50)
    w.include_comments.setChecked(True)
    run("Fetch, past week, 50 posts with comments", w._start_fetch)
    day = int((time.time() - 3 * 86400) // 86400 * 86400)
    run("Download all, one day", lambda: w.start_request(collect.FetchRequest(
        sub, after=day, before=day + 86400, limit=10000, include_comments=False, everything=True), "day"))
    ids = tuple(r["id"] for r in w.store.posts(sub) if day <= r["created_utc"] < day + 86400)[:40]
    run(f"Download all comments, {len(ids)} posts", lambda: w.start_request(
        collect.FetchRequest(sub, comment_posts=ids), "comments"))
    print(f"Saved {len(w.store.posts(sub))} posts and {len(w.store.comments(sub))} comments")
    print("HTTP statuses:", dict(statuses))
    print("4xx/5xx:", bad[:10] or "none")
    print("Warning dialogs:", warnings or "none")
    w.close()


if __name__ == "__main__":
    main()
