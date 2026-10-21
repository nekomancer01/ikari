"""A big saved subreddit (Ame's has 650,000 comments) opens without holding up the window or filling memory. Work
and memory are counted, not timed, so these hold on a slow runner too; tools/perf.py --db measures the frame times
on a real database."""
import os
import time
import tracemalloc

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui.worker import LoadWorker  # noqa: E402
from subsentiment.store import Store  # noqa: E402

from test_big_data import COMMENTS, POSTS, count_calls, fill  # noqa: E402
from test_responsive import until  # noqa: E402


def settings(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)


def test_the_app_shows_its_window_before_reading_a_big_subreddit(tmp_path, monkeypatch):
    settings(tmp_path)
    db = str(tmp_path / "big.db")
    fill(db)
    QSettings().setValue("ui/last_dataset", "Big")
    reads = [count_calls(monkeypatch, Store, name) for name in ("rows", "posts", "comments")]
    w = mw.MainWindow(db, wait_for_data=False)  # as the app opens it
    try:
        # Reading Ame's 650,000 comments took 17 s before the window first showed.
        assert reads == [[], [], []] and w.subheading.text() == "Opening r/Big…"
        w.show()
        until(lambda: len(w._saved["comments"]) == COMMENTS and not w._span_slices.running(), 30)
        assert w.current == "Big" and w.comments_tab.model.rowCount() == COMMENTS
    finally:
        w.close()
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_a_big_subreddit_takes_half_the_memory_it_did(tmp_path):
    db = str(tmp_path / "big.db")
    fill(db)
    got = []
    worker = LoadWorker(db, "Big", None)
    worker.finished.connect(got.append)
    tracemalloc.start()
    try:
        before = tracemalloc.get_traced_memory()[0]
        worker.run()  # as the window reads it, on this thread instead of its own
        held = tracemalloc.get_traced_memory()[0] - before
    finally:
        tracemalloc.stop()
    _, rows, analysis = got[0]
    assert len(rows["comments"]) == COMMENTS and analysis["comments"].count == COMMENTS
    # A dict per row took about 1,040 bytes a row here; a tuple of its values, with what rows share (post ids and
    # titles, labels, authors) kept once, about 530.
    assert held / (POSTS + COMMENTS) < 700
