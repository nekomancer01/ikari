"""The Download coverage window driven like a user would, with the real main window, fetch threads and
store, and a fake archive in place of Arctic Shift. Nothing may be downloaded unless a Download all
button is clicked, and each button fetches only what it says."""
import os
import threading
import time

from dataclasses import replace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QPushButton  # noqa: E402

from subsentiment.arctic_shift import keep  # noqa: E402
from subsentiment.gui import coverage, worker  # noqa: E402
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
    caps: list = []  # (cap, pick) of each thread asked for
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

    def fetch_comments(self, p, limit=100, pick="top"):
        FakeArchive.calls.append(("comments", p.id))
        return []

    def fetch_all_comments(self, p, cap=0, pick="top"):
        FakeArchive.calls.append(("all_comments", p.id))
        FakeArchive.caps.append((cap, pick))
        thread = [Comment(f"c{p.id}{j}", p.id, p.subreddit, "t3_" + p.id, "b", "ok", j, p.created_utc, 0, "u")
                  for j in range(5)]
        kept = keep(thread, cap, pick, p.id)
        return kept, len(kept) == len(thread)


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
    for sub in ("Python", "rust"):  # each fetched for the past week before: the days the page lists
        s.save_tried(sub, range(TODAY - 7 * DAY, TODAY + DAY, DAY))
    # The page lists only days with something saved: a skipped image post on each other day of the week (and
    # today) stands for that, without adding a post to any page.
    for k in (0, 4, 5, 6, 7):
        for sub in ("Python", "rust"):
            picture = post(f"{sub}{k}", sub, TODAY - k * DAY + 700)
            s.save_post(replace(picture, image=True), 0.0, "neutral")
    s.commit()
    s.close()
    FakeArchive.calls = []
    FakeArchive.caps = []
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
        idle = not w.fetch_job.busy and (cov is None or not (cov.count_job.busy or cov.delete_job.busy
                                                             or cov.reading()))
        if idle and until():
            break
        time.sleep(0.01)
    for _ in range(5):
        QApplication.processEvents()


def day_cell(cov, row: int, col: int):
    """A day row's widget in a column, the row scrolled into view first as a user would: Download coverage gives
    rows their widgets as they come near the view."""
    cov.day_table.scrollTo(cov.day_table.model().index(row, col))
    return cov.day_table.cellWidget(row, col)


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
    used = day_cell(c, c.days.index(day), 2).findChild(QPushButton)
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


def test_each_day_says_how_many_comment_threads_are_saved_whole_and_follows_downloads(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)

    def line(day):  # the day's Comment threads column
        return day_cell(c, c.days.index(day), 3).main
    day = TODAY - DAY  # p0 is on this day; p1 and p2 on the two before, each with 5 comments to get
    assert [line(TODAY - k * DAY).full_text() for k in (1, 2, 3)] == ["0 of 1\u00a0thread"] * 3
    assert line(TODAY).full_text() == "No posts saved"  # nothing saved today: no threads to count
    assert not day_cell(c, c.days.index(TODAY), 4).findChild(QPushButton).isEnabled()
    assert c.threads_total.text() == "Comments complete on 0 of 3\u00a0posts"
    c.day_table.selectRow(c.days.index(day))
    settle(w)
    click(c.post_table, 0)  # p0's comments, read to the end
    settle(w, lambda: not w.fetch_job.busy and line(day).full_text().startswith("\u2713"))
    assert line(day).full_text() == "\u2713 1\u00a0thread"
    assert c.threads_total.text() == "Comments complete on 1 of 3\u00a0posts"
    tick(c, TODAY - 2 * DAY)  # the total covers the ticked days once any are
    assert c.threads_total.text() == "Comments complete on 0 of 1\u00a0post"
    assert downloads() == [("all_comments", "p0")]
    narrow = coverage.FitLabel()  # a day of thousands of posts, in a column too narrow for the word
    narrow.resize(narrow.fontMetrics().horizontalAdvance("1,234 of 5,678\u00a0threads") - 1, 20)
    narrow.set("1,234 of 5,678\u00a0threads", "1,234 of 5,678")
    assert narrow.text() == "1,234 of 5,678"
    narrow.show()
    narrow.resize(narrow.width() + 2, 20)  # widened: the word comes back
    QApplication.processEvents()
    assert narrow.text() == "1,234 of 5,678\u00a0threads"
    narrow.close()


def test_a_long_history_opens_with_the_rows_on_screen_and_reads_off_the_window_thread(window, monkeypatch):
    """Two years of days and a day of 1,500 posts. Every day's five widgets were built as the page opened (three
    seconds for 711 days at the owner's size), and what's saved was read on the window's thread: now only the
    rows near the view are built, and the reads run on threads of their own."""
    w = window
    day = TODAY - DAY
    for d in range(TODAY - 729 * DAY, TODAY - DAY, DAY):  # a post on each day: the days listed are the ones saved
        w.store.save_post(post(f"old{d}", "Python", d + 300), 0.0, "neutral")
    for i in range(1500):
        w.store.save_post(post(f"busy{i}", "Python", day + 600 + i), 0.0, "neutral")
    w.store.commit()
    on_window = []  # reads of what Coverage shows made on the window's thread
    for name in ("downloaded_days", "tried_days", "posts_per_day", "removed_per_day", "archive_counts", "complete_keys",
                 "comment_coverage", "whole_threads_per_day"):
        real = getattr(Store, name)

        def read(self, *a, real=real, name=name, **k):
            if threading.current_thread() is threading.main_thread():
                on_window.append(name)
            return real(self, *a, **k)
        monkeypatch.setattr(Store, name, read)
    cells = []  # each day row has two, each post row one
    real_cell = coverage.Cell.__init__
    monkeypatch.setattr(coverage.Cell, "__init__", lambda self: (cells.append(1), real_cell(self))[1])
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    assert len(c.days) == 730 and c.day_table.rowCount() == 730
    c.day_table.selectRow(c.days.index(day))  # the busy day, opened
    settle(w, lambda: c.post_table.rowCount() == 1501)
    assert c.posts_title.text() == f"Posts on {coverage.short_day(day)} · 1,501"
    assert 0 < len(cells) < 100, len(cells)  # before: 1,460 for the days, as the page opened
    assert on_window == []  # before: every read but the threads' count
    c.day_table.scrollToBottom()  # scrolled to the oldest day: its row is built as it comes into view
    settle(w)
    assert len(cells) < 200
    oldest = c.days.index(TODAY - 729 * DAY)
    assert day_cell(c, oldest, 1).main.text() == "1 of 10"  # the stand-in archive counts 10 a day
    assert on_window == [] and downloads() == []


def tick(c, day, shift=False):
    """A real click on a day's tick box, with Shift held for a run of days."""
    box = day_cell(c, c.days.index(day), 0).findChild(QCheckBox)
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
    boxes = [day_cell(c, r, 0).findChild(QCheckBox) for r in range(c.day_table.rowCount())]
    assert all(b.isChecked() for b in boxes)
    w.show_page("overview")
    w.show_page("coverage")  # the page rebuilt its rows: the ticks stay
    settle(w)
    assert all(day_cell(c, r, 0).findChild(QCheckBox).isChecked() for r in range(c.day_table.rowCount()))
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
    QTest.mouseClick(c.what.buttons["posts"], Qt.MouseButton.LeftButton)
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
    assert c.what.value() == "both"  # the default
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


def test_download_selected_keeps_the_picked_number_of_comments_per_thread_and_tops_up_later(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    assert c.comment_cap.count.text() == "All comments"
    pick = c.comment_cap.pick
    items = {pick.itemData(k): pick.model().item(k).isEnabled() for k in range(pick.count())}
    assert items == {"top": False, "random": False, "sample": True}  # nothing left out to pick between; Sample is there
    pick.setCurrentIndex(pick.findData("sample"))  # the sample sets its own size; Top and Random are the way back
    assert not c.comment_cap.count.isEnabled() and pick.model().item(pick.findData("top")).isEnabled()
    pick.setCurrentIndex(pick.findData("top"))
    assert c.comment_cap.count.isEnabled()
    c.comment_cap.count.setValue(2)
    c.comment_cap.pick.setCurrentIndex(c.comment_cap.pick.findData("random"))
    tick(c, TODAY - DAY)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert FakeArchive.caps == [(2, "random")]
    picked = {x["id"] for x in w.store.comments("Python")}
    assert len(picked) == 2
    assert "p0" not in w.store.complete_keys("comments", "Python")  # 2 of its 5: not whole

    FakeArchive.calls, FakeArchive.caps = [], []
    tick(c, TODAY - DAY)  # the same day again at the same cap: its thread has its 2 already
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert not [d for d in downloads() if d[0] == "all_comments"]

    c.comment_cap.count.setValue(4)  # a bigger cap tops the thread up, keeping the random ones it had
    tick(c, TODAY - DAY)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert FakeArchive.caps == [(4, "random")]
    now = {x["id"] for x in w.store.comments("Python")}
    assert len(now) == 4 and picked < now
    assert "2 comments" in w.progress_msg.text().replace("\u00a0", " ")  # only the new ones count

    c.comment_cap.count.setValue(0)  # All: the rest of the thread, which is then whole
    tick(c, TODAY - DAY)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert len(w.store.comments("Python")) == 5 and "p0" in w.store.complete_keys("comments", "Python")
    settings = QSettings()
    assert (settings.value("coverage/comments_per_post", type=int), settings.value("coverage/comment_pick")) == (0, "random")


def test_a_cap_that_holds_a_whole_thread_marks_it_complete(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    c.comment_cap.count.setValue(50)
    tick(c, TODAY - DAY)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert FakeArchive.caps == [(50, "top")]
    assert "p0" in w.store.complete_keys("comments", "Python")  # all 5 fit under 50


def test_a_thread_holding_the_cap_counts_as_done_until_the_cap_is_raised(window):
    # The owner, 22 Oct: "fix the comment cap issue". With a cap set, a thread holding that many comments isn't
    # downloaded again, but the day's Comment threads still read "0 of 1 thread" and its ↓ asked again, forever.
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals and c._threads)
    c.comment_cap.count.setValue(3)
    day = TODAY - 2 * DAY
    row = c.days.index(day)
    c.day_table.selectRow(row)
    QTest.mouseClick(day_cell(c, row, 4).findChild(QPushButton), Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert FakeArchive.caps == [(3, "top")] and len(w.store.comments("Python")) == 3  # 3 of p1's 5
    cell = day_cell(c, row, 3)
    settle(w, lambda: cell.main.full_text().startswith("✓"))
    assert cell.main.full_text() == "✓ 1\u00a0thread"  # on main: "0 of 1 thread"
    assert day_cell(c, row, 4).findChild(QPushButton).text() == coverage.CHECK
    assert not c.all_comments.isEnabled()  # the opened day's posts offer nothing more at this cap
    assert c.post_table.cellWidget(0, 2).findChild(QPushButton).text() == coverage.CHECK
    c.comment_cap.count.setValue(4)  # a bigger cap: the thread is owed one more
    settle(w, lambda: not cell.main.full_text().startswith("✓"))
    assert cell.main.full_text() == "0 of 1\u00a0thread" and c.all_comments.isEnabled()


def test_comments_only_downloads_follow_the_cap_too(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    QTest.mouseClick(c.what.buttons["comments"], Qt.MouseButton.LeftButton)
    c.comment_cap.count.setValue(3)
    tick(c, TODAY - DAY)  # p0 is saved there, its thread not
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert FakeArchive.caps == [(3, "top")] and len(w.store.comments("Python")) == 3
    FakeArchive.calls, FakeArchive.caps = [], []
    tick(c, TODAY - DAY)  # it has its 3: nothing to ask
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert not [d for d in downloads() if d[0] == "all_comments"]
    c.comment_cap.count.setValue(0)  # All: the day's comments button finishes it
    c._download_day_threads(TODAY - DAY)
    settle(w, lambda: not w.fetch_job.busy)
    assert FakeArchive.caps == [(0, "top")] and "p0" in w.store.complete_keys("comments", "Python")


def test_the_comment_cap_is_remembered(tmp_path, window):
    w = window
    c = w.coverage
    QTest.mouseClick(c.what.buttons["posts"], Qt.MouseButton.LeftButton)
    assert not c.comment_cap.count.isEnabled()  # posts only: no comments to cap
    c.comment_cap.count.setValue(300)
    c.comment_cap.pick.setCurrentIndex(c.comment_cap.pick.findData("random"))
    again = mw.MainWindow(w.db_path)
    try:
        cap = again.coverage.comment_cap
        assert again.coverage.what.value() == "posts"
        assert (cap.count.value(), cap.pick.currentData()) == (300, "random")
        assert again.fetch_form.comments_per_post.value() == 50  # the Fetch drawer keeps its own
    finally:
        again.close()


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
    assert TODAY - 3 * DAY not in c.days  # nothing left saved on the day: it isn't listed
    assert "2 days" in w.statusBar().currentMessage().replace("\u00a0", " ")

    # Deleted, it stays deleted: not back when the subreddit is opened again.
    select(w, "rust")
    select(w, "Python")
    assert store.posts_per_day("Python", TODAY - 3 * DAY, TODAY - 2 * DAY) == {}
    assert TODAY - 3 * DAY not in c.days and downloads() == []


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
    assert TODAY - DAY not in c.days  # the day has nothing saved now: it isn't listed


def test_a_deleted_day_leaves_the_timeline_with_the_late_comments_written_on_it(window):
    """A comment counts on the day it was written, so a reply written on a deleted day to an older day's post
    goes with the day: nothing of it stays on the timeline, tiles, tables or words. The older post's thread
    keeps its record of being downloaded, so the reply isn't fetched back onto the deleted day."""
    w = window
    s = w.store
    s.save_comment(Comment("late", "p2", "Python", "t3_p2", "b", "zebrafish", 1, TODAY - DAY + 900, 0, "u"),
                   0.8, "positive")
    s.mark_complete("comments", "Python", "p2")
    s.commit()
    select(w, "rust")
    select(w, "Python")
    assert [d for d, _, _ in w.overview.timeline.points] == [TODAY - 3 * DAY, TODAY - 2 * DAY, TODAY - DAY]
    assert "zebrafish" in {word for word, _, _ in w.word_tally.top(["posts", "comments"], 500)}
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    tick(c, TODAY - DAY)  # p0's day, and the day "late" was written
    QTest.mouseClick(c.delete_ticked, Qt.MouseButton.LeftButton)
    settle(w)
    assert "every comment written on that day" in c.delete_question.text().replace("\u00a0", " ")
    answer(c, True)
    settle(w, lambda: not c.delete_job.busy)
    assert [d for d, _, _ in w.overview.timeline.points] == [TODAY - 3 * DAY, TODAY - 2 * DAY]
    assert sum(w._counted.counts.values()) == 2  # p1 and p2
    assert w.comments == [] and w.comments_tab.model.rowCount() == 0
    assert "zebrafish" not in {word for word, _, _ in w.word_tally.top(["posts", "comments"], 500)}
    assert "1 comment" in w.statusBar().currentMessage().replace("\u00a0", " ")
    assert "p2" in s.complete_keys("comments", "Python")
    assert downloads() == []


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
    assert not day_cell(c, 0, 2).findChild(QPushButton).isEnabled()
    assert not w.start_request(mw.collect.FetchRequest(subreddit="Python"), "x")
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.datasets.count())]
    w.datasets.setCurrentRow(names.index("rust"))  # another subreddit looked at meanwhile stays on screen
    QApplication.processEvents()
    hold.set()
    settle(w, lambda: not c.delete_job.busy)
    assert w.current == "rust" and c.subreddit == "rust"
    assert w.fetch_btn.isEnabled() and w.delete_btn.isEnabled()
    assert day_cell(c, 0, 2).findChild(QPushButton).isEnabled()
    assert w.store.posts_per_day("Python", TODAY - DAY, TODAY) == {}
    assert downloads() == []


def counts_asked():
    return [c for c in FakeArchive.calls if c[0] == "counts"]


def reopen(w):
    """The app closed and opened again on the same database, Python's coverage showing."""
    path = w.db_path
    w.close()
    again = mw.MainWindow(path)
    again.fetch_form.range.setCurrentIndex(again.fetch_form.range.findData("week"))
    again.show()
    select(again, "Python")
    again.show_page("coverage")
    settle(again)
    return again


def test_day_counts_are_kept_and_asked_for_again_only_the_next_day(window, monkeypatch):
    monkeypatch.setattr(coverage, "COUNT_SETTLED", 0)  # yesterday's count is kept too, whatever the hour
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals.get("python"))
    assert len(counts_asked()) == 1
    again = reopen(w)  # opened again the same day: the kept counts are shown, not asked for
    try:
        c = again.coverage
        assert len(counts_asked()) == 1
        row = c.days.index(TODAY - 2 * DAY)
        assert day_cell(c, row, 1).main.text() == "1 of 10"
        c.recount.click()  # Recount asks anyway
        settle(again)
        assert len(counts_asked()) == 2
        monkeypatch.setattr(coverage, "today", lambda: TODAY + DAY)  # the next UTC day
        c.refresh_saved()
        settle(again)
        assert len(counts_asked()) == 3
    finally:
        again.close()
    assert downloads() == []


def test_a_count_asked_for_before_the_archive_has_settled_its_day_isnt_kept(window, monkeypatch):
    monkeypatch.setattr(coverage, "COUNT_SETTLED", 30 * DAY)  # every listed day still settling
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals.get("python"))
    again = reopen(w)
    try:
        assert len(counts_asked()) == 2
    finally:
        again.close()


def test_comments_download_fetches_only_the_ticked_days_threads_not_their_posts(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    QTest.mouseClick(c.what.buttons["comments"], Qt.MouseButton.LeftButton)
    assert not c.download_ticked.isEnabled()  # nothing ticked
    tick(c, TODAY - DAY)
    tick(c, TODAY - 3 * DAY)
    assert "2\u00a0threads" in c.estimate.text() and c.download_ticked.isEnabled()
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert sorted(downloads()) == [("all_comments", "p0"), ("all_comments", "p2")]  # p1, on the day between, isn't
    assert c.ticked == set()
    assert QSettings().value("coverage/what") == "comments"  # remembered


def test_a_days_comments_button_fetches_that_days_threads_only(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals and c._threads)
    row = c.days.index(TODAY - 2 * DAY)
    button = day_cell(c, row, 4).findChild(QPushButton)
    assert button.text() == coverage.DOWNLOAD and button.isEnabled()
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy and day_cell(c, row, 3).main.full_text().startswith("✓"))
    assert downloads() == [("all_comments", "p1")]
    assert day_cell(c, row, 4).findChild(QPushButton).text() == coverage.CHECK


def test_the_filter_shows_only_days_missing_something_and_select_all_ticks_those(window):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals and c._threads)
    tick(c, TODAY)  # nothing saved today
    QTest.mouseClick(c.filter.buttons["comments"], Qt.MouseButton.LeftButton)
    assert c._shown_days() == [TODAY - k * DAY for k in (1, 2, 3)]  # the days with saved posts
    assert c.ticked == set()  # a day the filter hides is ticked no longer
    c.tick_all.click()
    assert c.ticked == {TODAY - k * DAY for k in (1, 2, 3)}
    QTest.mouseClick(c.filter.buttons["all"], Qt.MouseButton.LeftButton)
    assert len(c._shown_days()) == len(c.days)
    assert downloads() == []


def test_a_stopped_download_offers_resume_even_after_a_restart(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    reads = FakeArchive.fetch_posts

    def stop_after_one(self, *a, **kw):
        got = reads(self, *a, **kw)
        w.fetch_job.stop()
        return got
    monkeypatch.setattr(FakeArchive, "fetch_posts", stop_after_one)
    QTest.mouseClick(c.what.buttons["posts"], Qt.MouseButton.LeftButton)
    assert not c.resume_bar.isVisible()
    tick(c, TODAY - DAY)
    tick(c, TODAY - 3 * DAY, shift=True)
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert c.resume_bar.isVisible() and "3\u00a0days" in c.resume_text.text()
    monkeypatch.setattr(FakeArchive, "fetch_posts", reads)
    FakeArchive.calls = []
    again = reopen(w)
    try:
        c = again.coverage
        assert c.resume_bar.isVisible()
        QTest.mouseClick(c.resume, Qt.MouseButton.LeftButton)
        settle(again, lambda: not again.fetch_job.busy)
        assert [d[2] for d in downloads() if d[0] == "posts"] == [TODAY - k * DAY for k in (1, 2, 3)]
        assert not c.resume_bar.isVisible() and c.ticked == set()
    finally:
        again.close()


def test_a_day_with_nothing_saved_isnt_listed_ticked_or_downloaded(window):
    asked = TODAY - 9 * DAY  # a fetch asked for it and the archive gave nothing
    window.store.save_tried("Python", [asked])
    window.store.commit()
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals and c._threads)
    assert asked not in c.days and TODAY in c.days  # today's partial day is listed: something is saved
    assert c.subtitle.full_text().endswith("8\u00a0days downloaded")
    QTest.mouseClick(c.what.buttons["posts"], Qt.MouseButton.LeftButton)
    c.tick_all.click()
    assert c.download_ticked.text() == "Download 8\u00a0days"
    QTest.mouseClick(c.download_ticked, Qt.MouseButton.LeftButton)
    settle(w, lambda: not w.fetch_job.busy)
    assert asked not in [d[2] for d in downloads() if d[0] == "posts"]


def test_the_once_a_second_refresh_during_a_download_redraws_only_the_days_that_changed(window, monkeypatch):
    # Every cell of every listed day was filled in again each second a fetch ran: a fifth of a second for two years.
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    cells = {id(day_cell(c, row, col)): (day, col) for row, day in enumerate(c.days) for col in (1, 3)}
    drawn = []
    for name in ("set", "set_open", "show_share"):
        real = getattr(coverage.Cell, name)
        monkeypatch.setattr(coverage.Cell, name,
                            lambda self, *a, real=real, **k: drawn.append(cells.get(id(self))) or real(self, *a, **k))
    c.refresh_saved()
    settle(w)
    assert drawn == []  # nothing changed
    day = TODAY - 3 * DAY
    s = Store(w.db_path)
    s.save_post(post("p-new", "Python", day + 900), 0.0, "neutral")  # as a running fetch saves one
    s.commit()
    s.close()
    c.refresh_saved()
    settle(w)
    assert (day, 1) in drawn and {d for d, _ in drawn} == {day}  # its posts, and its threads once they're counted
    assert day_cell(c, c.days.index(day), 1).main.full_text().startswith("2")


def test_row_buttons_sit_whole_and_centred_in_their_cells(window):
    """The cell a table gives a row button is its column less the theme's item padding: a button set off by
    margins of its own ran past the cell's right edge and lost its border there."""
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    c.day_table.selectRow(1)
    settle(w, lambda: c.post_table.rowCount())
    for t, cols in ((c.day_table, (2, 4)), (c.post_table, (2,))):
        w._apply_theme()  # cell widgets take the delegate's geometry once the rows are restyled
        settle(w)
        assert t.cellWidget(0, cols[0]).width() < t.columnWidth(cols[0])  # the theme's item padding is off it
        for row in range(t.rowCount()):
            for col in cols:
                t.scrollTo(t.model().index(row, col))  # a row gets its widgets as it comes near the view
                box = t.cellWidget(row, col)
                b = box.findChild(QPushButton)
                assert box.rect().contains(b.geometry()), (row, col, box.rect(), b.geometry())
                assert abs(b.geometry().center().x() - box.rect().center().x()) <= 1


def held_posts(monkeypatch):
    """The first posts download waits until the returned event is set, so the test can click while it runs."""
    import threading
    hold = threading.Event()
    reads = FakeArchive.fetch_posts
    first = []

    def held(self, *a, **kw):
        if not first:
            first.append(1)
            hold.wait(5)
        return reads(self, *a, **kw)
    monkeypatch.setattr(FakeArchive, "fetch_posts", held)
    return hold


def row_button(c, day, col=2):
    return c.day_table.cellWidget(c.days.index(day), col).findChild(coverage.RowButton)


def test_a_row_button_clicked_during_a_download_queues_it_and_a_second_click_takes_it_out(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    hold = held_posts(monkeypatch)
    one, two, three = TODAY - DAY, TODAY - 2 * DAY, TODAY - 3 * DAY
    QTest.mouseClick(row_button(c, one), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    assert w.fetch_job.busy
    for day in (two, three):  # queued while the first runs: still clickable, and nothing more is fetched yet
        assert row_button(c, day).isEnabled()
        QTest.mouseClick(row_button(c, day), Qt.MouseButton.LeftButton)
    assert row_button(c, two).queued and row_button(c, three).queued and not row_button(c, one).queued
    assert "Queued" in row_button(c, two).toolTip()
    QTest.mouseClick(row_button(c, two), Qt.MouseButton.LeftButton)  # clicked again: out of the queue
    assert not row_button(c, two).queued and row_button(c, three).queued
    hold.set()
    settle(w, lambda: not c._queue and len(downloads()) == 2)
    assert downloads() == [("posts", "Python", one, one + DAY), ("posts", "Python", three, three + DAY)]
    assert not any(row_button(c, d).queued for d in (one, two, three))
    assert w.store.posts_per_day("Python", three, three + DAY) == {three: 5}


def test_a_queued_days_comments_take_in_the_posts_downloaded_before_them(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals and c._threads)
    hold = held_posts(monkeypatch)
    day = TODAY - 3 * DAY  # p2 is saved; the archive has four more
    QTest.mouseClick(row_button(c, day), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    QTest.mouseClick(row_button(c, day, 4), Qt.MouseButton.LeftButton)
    assert row_button(c, day, 4).queued
    hold.set()
    settle(w, lambda: not c._queue and len(downloads()) > 1)
    assert downloads()[0] == ("posts", "Python", day, day + DAY)
    assert sorted(downloads()[1:]) == sorted(("all_comments", p) for p in ("p2", "new0", "new1", "new2", "new3"))


def test_stop_also_drops_the_queued_downloads(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    hold = held_posts(monkeypatch)
    QTest.mouseClick(row_button(c, TODAY - DAY), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    QTest.mouseClick(row_button(c, TODAY - 2 * DAY), Qt.MouseButton.LeftButton)
    assert row_button(c, TODAY - 2 * DAY).queued
    w._stop_fetch()
    hold.set()
    settle(w)
    assert not row_button(c, TODAY - 2 * DAY).queued
    assert [d[2] for d in downloads()] == [TODAY - DAY]


def test_a_queued_button_turns_and_is_lighter_and_stays_still_with_motion_off(window, monkeypatch):
    w = window
    select(w, "Python")
    w.show_page("coverage")
    c = w.coverage
    settle(w, lambda: c.totals)
    hold = held_posts(monkeypatch)
    QTest.mouseClick(row_button(c, TODAY - DAY), Qt.MouseButton.LeftButton)
    QApplication.processEvents()
    b = row_button(c, TODAY - 2 * DAY)
    still = b.grab().toImage()
    QTest.mouseClick(b, Qt.MouseButton.LeftButton)
    # The arrow's angle follows the shared throbber clock; set it rather than wait on it (CI runners may have
    # the OS's animations off, which turns motion off).
    for phase in (0.0, 0.25, None):
        monkeypatch.setattr(coverage.motion, "throb", lambda _w, p=phase: p)
        shot = b.grab().toImage()
        if phase == 0.0:
            upright = shot
            assert shot != still  # lighter
        elif phase:
            assert shot != upright  # turned a quarter
        else:
            assert shot == upright and shot != still  # motion off: lighter, but still
    hold.set()
    settle(w, lambda: not c._queue)
