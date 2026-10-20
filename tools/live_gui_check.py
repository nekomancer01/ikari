"""Runs the real app window (offscreen) against the live Arctic Shift API and reports what happened.

Run from the repo root:  QT_QPA_PLATFORM=offscreen python tools/live_gui_check.py [subreddit] [days back] [seconds per step]
Does a normal fetch, then opens View > Download coverage, prints each day's row once the archive's counts
are in, and clicks that window's own Download all for one day and Download all comments for it, all into a
throwaway database and settings folder. Prints every HTTP status the archive sent, any 4xx/5xx, warning dialogs,
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

from PySide6.QtWidgets import QPushButton  # noqa: E402

from subsentiment.gui import coverage, main_window as mw  # noqa: E402

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
    back = int(sys.argv[2]) if len(sys.argv) > 2 else 3  # which day's Download all to click, in days back
    cap = float(sys.argv[3]) if len(sys.argv) > 3 else 600  # seconds before a step presses Stop
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
        t0, spent[0], worst = time.time(), 0.0, 0.0
        start()
        stopped = False
        while w.thread is not None:
            if not stopped and time.time() - t0 > cap:
                w._stop_fetch()
                stopped = True
            t = time.perf_counter()
            app.processEvents()
            worst = max(worst, time.perf_counter() - t)  # how long the window went without handling input
            time.sleep(0.005)
        for _ in range(50):
            app.processEvents()
            time.sleep(0.01)
        model = w.posts_tab.model
        subs = {model.row_at(i)["subreddit"].lower() for i in range(model.rowCount())}
        print(f"{label}: {time.time() - t0:.0f} s, {spent[0]:.1f} s reading rows back, worst freeze {worst:.1f} s; "
              f"tables show {sorted(subs)}; {w.progress_msg.text()}")

    w.subreddit.setText(sub)
    w.limit.setValue(50)
    w.include_comments.setChecked(False)  # comments come from the coverage window below
    run("Fetch, past week, 50 posts", w._start_fetch)
    w.coverage_act.trigger()
    cov = w.coverage
    t0 = time.time()
    while (cov._count_thread is not None or not cov.days) and time.time() - t0 < 180:
        app.processEvents()
        time.sleep(0.01)

    def rows():
        for row, d in enumerate(cov.days):
            cell = cov.day_table.cellWidget(row, 1)
            side = cell.note.text() if cell.note.isVisible() else cell.side.text()
            gone = w.store.removed_per_day(sub, d, d + 86400).get(d, 0)
            print(f"  {coverage.day_text(d)}: {cell.main.text()} | {side} | "
                  f"archive count {cov.totals.get(sub.lower(), {}).get(d)}, removed posts seen {gone}")
    print(f"Coverage window: counts in after {time.time() - t0:.0f} s, error: {cov.count_error or 'none'}")
    rows()
    row = back  # a settled day
    day = cov.days[row]
    cov.day_table.selectRow(row)
    run(f"Coverage Download all, {coverage.day_text(day)}",
        cov.day_table.cellWidget(row, 2).findChild(QPushButton).click)
    cov.day_table.selectRow(row)
    app.processEvents()
    if cov.all_comments.isEnabled():
        run(f"Coverage {cov.all_comments.text()}", cov.all_comments.click)
    else:
        print(f"Coverage {cov.all_comments.text()}: disabled, nothing to download")
    print("Coverage after:")
    rows()
    print(f"Saved {len(w.store.posts(sub))} posts and {len(w.store.comments(sub))} comments")
    print("HTTP statuses:", dict(statuses))
    print("4xx/5xx:", bad[:10] or "none")
    print("Warning dialogs:", warnings or "none")
    w.close()


if __name__ == "__main__":
    main()
