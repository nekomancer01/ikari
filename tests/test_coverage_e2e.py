"""The Download coverage window driven like a user would, with the real main window, fetch threads and
store, and a fake archive in place of Arctic Shift. Nothing may be downloaded unless a Download all
button is clicked, and each button fetches only what it says."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from subsentiment.gui import worker  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.reddit import Comment, Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

DAY = 86400
TODAY = int(time.time() // DAY * DAY)


def post(pid, sub, created, num_comments=5):
    return Post(pid, sub, f"title {pid}", "", "a", 3, 0.9, num_comments, created, f"https://r/{pid}", "")


class FakeArchive:
    """Stands in for ArcticShiftClient; every call is logged on the class."""
    calls: list = []
    archive = {"Python": [post(f"new{i}", "Python", TODAY - 3 * DAY + 100 + i) for i in range(4)]}

    def __init__(self, limiter=None, **kw):
        self.limiter = type("L", (), {"on_wait": None})()

    def day_counts(self, subreddit, after, before, should_stop=None):
        FakeArchive.calls.append(("counts", subreddit))
        return {d: 10 for d in range(int(after // DAY * DAY), int(before), DAY)}

    def fetch_posts(self, subreddit, after, before, limit, query="", top=False, skip=(), progress=None,
                    should_stop=None):
        FakeArchive.calls.append(("posts", subreddit, after, before))
        return [p for p in self.archive.get(subreddit, []) if after <= p.created_utc < before and p.id not in skip]

    def fetch_posts_by_ids(self, ids, should_stop=None):
        FakeArchive.calls.append(("ids", list(ids)))
        return []

    def fetch_comments(self, p, limit=100):
        FakeArchive.calls.append(("comments", p.id))
        return []

    def fetch_all_comments(self, p):
        FakeArchive.calls.append(("all_comments", p.id))
        return [Comment(f"c{p.id}{j}", p.id, p.subreddit, "t3_" + p.id, "b", "ok", 1, p.created_utc, 0, "u")
                for j in range(5)], True


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    QSettings().setValue("fetch/include_comments", True)  # the sidebar would fetch comments; coverage mustn't
    db = tmp_path / "e2e.db"
    s = Store(db)
    for i in range(3):
        s.save_post(post(f"p{i}", "Python", TODAY - (i + 1) * DAY + 500), 0.0, "neutral")
    s.save_post(post("r0", "rust", TODAY - DAY + 500), 0.0, "neutral")
    s.commit()
    s.close()
    FakeArchive.calls = []
    monkeypatch.setattr(worker, "ArcticShiftClient", FakeArchive)
    w = mw.MainWindow(str(db))
    w.range.setCurrentIndex(w.range.findData("week"))
    w.show()
    yield w
    w.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def settle(w, until=lambda: True, timeout=5.0):
    """Run the event loop until no fetch or count is running and ``until`` holds."""
    end = time.time() + timeout
    while time.time() < end:
        QApplication.processEvents()
        cov = w.coverage
        idle = w.thread is None and (cov is None or cov._count_thread is None)
        if idle and until():
            break
        time.sleep(0.01)
    for _ in range(5):
        QApplication.processEvents()


def downloads():
    return [c for c in FakeArchive.calls if c[0] != "counts"]


def select(w, name):
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.datasets.count())]
    w.datasets.setCurrentRow(names.index(name))
    settle(w)


def click(table, row):
    """A real mouse click on a row's Download all, so focus moves the way it does for a user."""
    QTest.mouseClick(table.cellWidget(row, 2).findChild(QPushButton), Qt.MouseButton.LeftButton)


def test_browsing_the_window_never_downloads_anything(window):
    w = window
    select(w, "Python")
    w._show_coverage()
    c = w.coverage
    settle(w, lambda: c.totals)
    for row in range(c.day_table.rowCount()):  # look at every day
        c.day_table.selectRow(row)
        settle(w)
    QTest.mouseClick(c.day_table.viewport(), Qt.MouseButton.LeftButton)  # a click on the list, not a button
    for key in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
        for target in (c, c.day_table, c.post_table):
            QTest.keyClick(target, key)
            settle(w)
    select(w, "rust")  # another subreddit, then back
    select(w, "Python")
    c.recount.click()
    settle(w)
    c.hide()
    w._show_coverage()  # closed and opened again
    settle(w)
    w._apply_theme()
    settle(w)
    assert downloads() == []
    assert w.thread is None


def test_a_days_download_all_fetches_that_days_posts_and_no_comments(window):
    w = window
    select(w, "Python")
    w._show_coverage()
    c = w.coverage
    settle(w, lambda: c.totals)
    day = TODAY - 3 * DAY
    click(c.day_table, c.days.index(day))
    settle(w, lambda: w.thread is None)
    assert downloads() == [("posts", "Python", day, day + DAY)]
    assert w.store.posts_per_day("Python", day, day + DAY) == {day: 5}  # one saved before, four new
    # Enter afterwards doesn't run it again. In a dialog the last clicked button would otherwise become
    # the default, so give it focus as a real click does (the offscreen platform doesn't) and press Enter.
    used = c.day_table.cellWidget(c.days.index(day), 2).findChild(QPushButton)
    assert used.focusPolicy() == Qt.FocusPolicy.NoFocus  # a click doesn't leave it focused, so Space can't either
    c.activateWindow()
    used.setFocus(Qt.FocusReason.MouseFocusReason)
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        for target in (c, c.day_table):
            QTest.keyClick(target, key)
            settle(w)
    assert len(downloads()) == 1


def test_a_posts_download_all_fetches_only_that_posts_comments(window):
    w = window
    select(w, "Python")
    w._show_coverage()
    c = w.coverage
    settle(w, lambda: c.totals)
    day = TODAY - DAY  # p0 is on this day
    c.day_table.selectRow(c.days.index(day))
    settle(w)
    assert c.post_table.rowCount() == 1
    click(c.post_table, 0)
    settle(w, lambda: w.thread is None)
    assert downloads() == [("all_comments", "p0")]
    cell = c.post_table.cellWidget(0, 1)
    assert cell.main.text() == "5 saved" and "Complete" in cell.side.text()
