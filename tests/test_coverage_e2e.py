"""The Download coverage window driven like a user would, with the real main window, fetch threads and
store, and a fake archive in place of Arctic Shift. Nothing may be downloaded unless a Download all
button is clicked, and each button fetches only what it says."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton  # noqa: E402

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
                    should_stop=None, on_posts=None, exhaust=False, read=()):
        self.exhausted = True
        FakeArchive.calls.append(("posts", subreddit, after, before))
        posts = [p for p in self.archive.get(subreddit, []) if after <= p.created_utc < before and p.id not in skip]
        if on_posts:
            on_posts(posts)
        return posts

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
    # A failed fetch would open a modal box and hang the test; fail fast instead.
    monkeypatch.setattr(mw.QMessageBox, "warning", lambda *a: pytest.fail(f"fetch failed: {a[-1]}"))
    w = mw.MainWindow(str(db))
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
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
        idle = not w.fetch_job.busy and (cov is None or not (cov.count_job.busy or cov.delete_job.busy))
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


def test_browsing_the_page_never_downloads_anything(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
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
    w.show_page("overview")
    w.show_page("coverage")  # left and opened again
    settle(w)
    w._apply_theme()
    settle(w)
    assert downloads() == []
    assert not w.fetch_job.busy


def test_a_days_download_all_fetches_that_days_posts_and_no_comments(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    day = TODAY - 3 * DAY
    click(c.day_table, c.days.index(day))
    settle(w, lambda: not w.fetch_job.busy)
    assert downloads() == [("posts", "Python", day, day + DAY)]
    assert w.store.posts_per_day("Python", day, day + DAY) == {day: 5}  # one saved before, four new
    # Enter afterwards doesn't run it again: give it focus as a real click does (the offscreen platform
    # doesn't) and press Enter.
    used = c.day_table.cellWidget(c.days.index(day), 2).findChild(QPushButton)
    assert used.focusPolicy() == Qt.FocusPolicy.NoFocus  # a click doesn't leave it focused, so Space can't either
    w.activateWindow()
    used.setFocus(Qt.FocusReason.MouseFocusReason)
    for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
        for target in (c, c.day_table):
            QTest.keyClick(target, key)
            settle(w)
    assert len(downloads()) == 1


def test_a_posts_download_all_fetches_only_that_posts_comments(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    day = TODAY - DAY  # p0 is on this day
    c.day_table.selectRow(c.days.index(day))
    settle(w)
    assert c.post_table.rowCount() == 1
    click(c.post_table, 0)
    settle(w, lambda: not w.fetch_job.busy)
    assert downloads() == [("all_comments", "p0")]
    cell = c.post_table.cellWidget(0, 1)
    assert cell.main.text() == "5 saved" and "Complete" in cell.side.text()


def tick(c, day, shift=False):
    """A real click on a day's tick box, with Shift held for a run of days."""
    box = c.day_table.cellWidget(c.days.index(day), 0).findChild(QCheckBox)
    QTest.mouseClick(box, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier)
    settle(c.window_)


def test_ticking_days_downloads_nothing_until_download_selected(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    assert not c.download_ticked.isEnabled()
    tick(c, TODAY - DAY)
    tick(c, TODAY - 4 * DAY, shift=True)  # a run: four days
    assert c.ticked == {TODAY - k * DAY for k in range(1, 5)}
    assert c.download_ticked.text() == "Download 4\u00a0days" and c.download_ticked.isEnabled()
    tick(c, TODAY - 2 * DAY)  # one unticked again
    assert c.ticked == {TODAY - DAY, TODAY - 3 * DAY, TODAY - 4 * DAY}
    c.tick_all.click()
    assert c.ticked == set(c.days) and c.tick_all.text() == "Select none"
    boxes = [c.day_table.cellWidget(r, 0).findChild(QCheckBox) for r in range(c.day_table.rowCount())]
    assert all(b.isChecked() for b in boxes)
    w.show_page("overview")
    w.show_page("coverage")  # the page rebuilt its rows: the ticks stay
    settle(w)
    assert all(c.day_table.cellWidget(r, 0).findChild(QCheckBox).isChecked() for r in range(c.day_table.rowCount()))
    c.tick_all.click()
    assert c.ticked == set() and c.tick_all.text() == "Select all"
    tick(c, TODAY - DAY)
    select(w, "rust")  # another subreddit starts with nothing ticked
    assert c.ticked == set() and not c.download_ticked.isEnabled()
    assert downloads() == []


def test_download_selected_fetches_only_the_ticked_days(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    c.with_comments.setChecked(False)
    tick(c, TODAY - 3 * DAY)
    tick(c, TODAY - DAY)  # not a run: the day between stays out
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert downloads() == [("posts", "Python", TODAY - DAY, TODAY), ("posts", "Python", TODAY - 3 * DAY, TODAY - 2 * DAY)]
    assert w.store.posts_per_day("Python", TODAY - 3 * DAY, TODAY - 2 * DAY) == {TODAY - 3 * DAY: 5}
    assert c.ticked == set()  # all read, so unticked for the next batch


def test_a_stopped_download_selected_keeps_its_ticks(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    reads = FakeArchive.fetch_posts

    def stop_after_one(self, *a, **kw):
        got = reads(self, *a, **kw)
        w.fetch_job.stop()  # Stop pressed while the first day is read
        return got
    monkeypatch.setattr(FakeArchive, "fetch_posts", stop_after_one)
    tick(c, TODAY - DAY)
    tick(c, TODAY - 3 * DAY, shift=True)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert len([d for d in downloads() if d[0] == "posts"]) == 1
    assert c.ticked == {TODAY - DAY, TODAY - 2 * DAY, TODAY - 3 * DAY}
    assert c.download_ticked.isEnabled()


def test_download_selected_says_what_comments_it_added(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    tick(c, TODAY - DAY)  # its one post is saved already, so only comments are new
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert "5 comments" in w.progress_msg.text().replace("\u00a0", " ")


def test_download_selected_with_comments_fetches_every_comment_on_those_days_only(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    assert c.with_comments.isChecked()
    tick(c, TODAY - 3 * DAY)
    tick(c, TODAY - DAY)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    got = downloads()
    assert [d for d in got if d[0] == "posts"] == [("posts", "Python", TODAY - DAY, TODAY),
                                                   ("posts", "Python", TODAY - 3 * DAY, TODAY - 2 * DAY)]
    threads = sorted(d[1] for d in got if d[0] == "all_comments")
    assert threads == sorted(["p0", "p2", "new0", "new1", "new2", "new3"])  # p1, on the day between, isn't
    assert not [d for d in got if d[0] == "comments"]
    assert "p1" not in w.store.complete_keys("comments", "Python")


def answer(c, yes, wait=True):
    """Answer the question Delete selected asks under the Days table, and let what it starts end."""
    assert c.delete_question.isVisible() and not c.delete_ticked.isVisible()
    QTest.mouseClick(c.delete_confirm if yes else c.delete_cancel, Qt.MouseButton.LeftButton)
    if wait:
        settle(c.window_)
    QApplication.processEvents()


def test_delete_selected_asks_then_forgets_only_the_ticked_days_and_they_download_again(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    tick(c, TODAY - 3 * DAY)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)  # posts and comments of one day saved first
    settle(w, lambda: not w.fetch_job.busy)
    assert w.store.posts_per_day("Python", TODAY - 3 * DAY, TODAY - 2 * DAY) == {TODAY - 3 * DAY: 5}
    assert str(TODAY - 3 * DAY) in w.store.complete_keys("posts", "Python")
    rust = (w.store.posts("rust"), w.store.comments("rust"))
    FakeArchive.calls = []

    assert not c.delete_ticked.isEnabled()
    tick(c, TODAY - 3 * DAY)
    tick(c, TODAY - DAY)
    assert c.delete_ticked.text().replace("\u00a0", " ") == "Delete 2 days…" and c.delete_ticked.isEnabled()
    QTest.mouseClick(c.delete_ticked, Qt.MouseButton.LeftButton)
    settle(w)
    asked = c.delete_question.text().replace("\u00a0", " ")
    assert "6 posts" in asked and "2 days" in asked and "r/Python" in asked and "can't be undone" in asked
    assert c.delete_confirm.text().replace("\u00a0", " ") == "Delete 2 days"
    tick(c, TODAY - 2 * DAY)  # ticked while it asks: the question follows
    assert "3 days" in c.delete_question.text().replace("\u00a0", " ")
    tick(c, TODAY - 2 * DAY)
    answer(c, False)  # nothing goes
    assert not c.delete_question.isVisible() and c.delete_ticked.isVisible()
    assert w.store.posts_per_day("Python", TODAY - 7 * DAY, TODAY + DAY) == {
        TODAY - DAY: 1, TODAY - 2 * DAY: 1, TODAY - 3 * DAY: 5}

    QTest.mouseClick(c.delete_ticked, Qt.MouseButton.LeftButton)
    settle(w)
    answer(c, True)
    settle(w, lambda: not c.delete_job.busy)
    assert downloads() == []  # deleting fetches nothing
    store = w.store
    assert store.posts_per_day("Python", TODAY - 7 * DAY, TODAY + DAY) == {TODAY - 2 * DAY: 1}  # the day between stays
    assert [r["post_id"] for r in store.comments("Python")] == []
    assert str(TODAY - 3 * DAY) not in store.complete_keys("posts", "Python")
    assert not store.complete_keys("comments", "Python")
    assert (store.posts("rust"), store.comments("rust")) == rust
    assert c.ticked == set()
    # Every page shows what's left, at once.
    assert [r["id"] for r in w.posts] == ["p1"] and w.comments == []
    assert w.posts_tab.model.rowCount() == 1 and w.comments_tab.model.rowCount() == 0
    assert w._counted.counts and sum(w._counted.counts.values()) == 1
    row = c.days.index(TODAY - 3 * DAY)
    assert c.day_table.cellWidget(row, 1).main.text() == "0 of 10"
    button = c.day_table.cellWidget(row, 2).findChild(QPushButton)
    assert button.text() == "Download all" and button.isEnabled()
    assert "2 days" in w.statusBar().currentMessage().replace("\u00a0", " ")

    # Deleted, it stays deleted: not back when the subreddit is opened again, and a download reads it in full.
    select(w, "rust")
    select(w, "Python")
    assert store.posts_per_day("Python", TODAY - 3 * DAY, TODAY - 2 * DAY) == {}
    click(c.day_table, row)
    settle(w, lambda: not w.fetch_job.busy)
    assert downloads() == [("posts", "Python", TODAY - 3 * DAY, TODAY - 2 * DAY)]
    assert store.posts_per_day("Python", TODAY - 3 * DAY, TODAY - 2 * DAY) == {TODAY - 3 * DAY: 4}


def test_deleting_every_saved_day_keeps_the_subreddit_on_screen_to_download_again(window):
    w = window
    select(w, "rust")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    tick(c, TODAY - DAY)  # r0, its only post
    QTest.mouseClick(c.delete_ticked, Qt.MouseButton.LeftButton)
    settle(w)
    answer(c, True)
    settle(w, lambda: not c.delete_job.busy)
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.datasets.count())]
    assert names == ["Python"] and w.current == "rust" and c.subreddit == "rust"
    assert w.posts == [] and w.store.posts("Python")
    assert downloads() == []
    FakeArchive.archive = dict(FakeArchive.archive, rust=[post("r0", "rust", TODAY - DAY + 500)])
    try:
        click(c.day_table, c.days.index(TODAY - DAY))
        settle(w, lambda: not w.fetch_job.busy)
    finally:
        del FakeArchive.archive["rust"]
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.datasets.count())]
    assert "rust" in names and [r["id"] for r in w.posts] == ["r0"]


def test_nothing_else_writes_while_days_are_deleted(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    tick(c, TODAY - DAY)
    QTest.mouseClick(c.delete_ticked, Qt.MouseButton.LeftButton)
    settle(w)
    import threading
    hold = threading.Event()
    deleting = Store.delete_days
    monkeypatch.setattr(Store, "delete_days", lambda self, *a: (hold.wait(5), deleting(self, *a))[1])
    answer(c, True, wait=False)
    assert c.delete_job.busy
    assert not w.fetch_btn.isEnabled() and not c.download_ticked.isEnabled() and not w.delete_btn.isEnabled()
    assert not c.day_table.cellWidget(0, 2).findChild(QPushButton).isEnabled()
    assert not w.start_request(mw.collect.FetchRequest(subreddit="Python"), "x")
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.datasets.count())]
    w.datasets.setCurrentRow(names.index("rust"))  # another subreddit looked at meanwhile stays on screen
    QApplication.processEvents()
    hold.set()
    settle(w, lambda: not c.delete_job.busy)
    assert w.current == "rust" and c.subreddit == "rust"
    assert w.fetch_btn.isEnabled() and w.delete_btn.isEnabled()
    assert c.day_table.cellWidget(0, 2).findChild(QPushButton).isEnabled()
    assert w.store.posts_per_day("Python", TODAY - DAY, TODAY) == {}
    assert downloads() == []
