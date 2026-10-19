"""The whole app driven like a user would: the real main window, fetch threads, HTTP client and store,
talking to a fake Arctic Shift server on localhost. The rule checked throughout: nothing is requested
unless the user asked for it, and each action requests only what it says."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSize, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from subsentiment import arctic_shift  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import worker  # noqa: E402
from subsentiment.store import Store  # noqa: E402

from fake_archive import FakeArchive, make_posts  # noqa: E402

NOW = time.time()
SEARCH, TREE, IDS, AGG = "/api/posts/search", "/api/comments/tree", "/api/posts/ids", "/api/posts/search/aggregate"


def pump(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def wait_idle(w, timeout: float = 30) -> None:
    """Until the fetch the window is running has finished and its thread is cleaned up."""
    end = time.monotonic() + timeout
    while w.thread is not None:
        assert time.monotonic() < end, "fetch didn't finish"
        QApplication.processEvents()
        time.sleep(0.01)
    pump(50)


@pytest.fixture
def env(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    archive = FakeArchive(make_posts("Python", NOW) + make_posts("rust", NOW, per_day=(3,), seed=2)
                          + make_posts("AskReddit", NOW, days=3, per_day=(150,), seed=3),
                          busy={"AskReddit"})
    monkeypatch.setattr(arctic_shift, "BASE_URL", archive.url)
    # Time runs ten times faster than in the app: requests every 0.04 s against a server allowing 30 a
    # second, and pauses for timeouts and limits capped at 1.2 s (the app's MAX_WAIT is 120 s).
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 1.2)
    monkeypatch.setattr(worker.LIMITER, "base", arctic_shift.BASE_INTERVAL / 10)
    monkeypatch.setattr(worker.LIMITER, "interval", arctic_shift.BASE_INTERVAL / 10)
    archive.rate = (30, 1.0)
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2] if len(a) > 2 else a))
    windows = []

    class Env:
        db = str(tmp_path / "e2e.db")

        def __init__(self):
            self.archive, self.warnings = archive, warnings

        def window(self):
            w = mw.MainWindow(self.db)
            w.show()
            windows.append(w)
            pump(50)
            return w

        def store(self):
            return Store(self.db)
    yield Env()
    for w in windows:
        w.close()
    archive.close()
    assert archive.violations() == []  # nothing the real API would refuse was sent
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def fetch(w, sub="python", comments=True, limit=100, rng="week", **extra):
    w.subreddit.setText(sub)
    w.include_comments.setChecked(comments)
    w.limit.setValue(limit)
    w.range.setCurrentIndex(w.range.findData(rng))
    for key, value in extra.items():
        getattr(w, key).setChecked(value)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    wait_idle(w)


def test_starting_and_browsing_never_downloads_anything(env):
    w = env.window()
    fetch(w, comments=False)
    fetch(w, sub="rust", comments=False)
    w.close()
    env.archive.clear()

    w = env.window()  # a restart, with saved data and remembered settings
    pump(400)
    for row in range(w.datasets.count()):  # switch between subreddits
        w.datasets.setCurrentRow(row)
        pump(50)
    for i in range(3):  # every tab, and every Overview scope
        w.tabbar.setCurrentIndex(i)
    for i in range(w.scope.count()):
        w.scope.setCurrentIndex(i)
    for size in (QSize(1280, 750), QSize(900, 600), QSize(1900, 1100)):
        w.resize(size)
        pump(30)
    w.showMaximized()
    pump(1200)  # longer than the redraw timer's 1 s
    assert env.archive.paths() == []
    assert w.thread is None


def test_a_fetch_without_comments_asks_for_no_comments(env):
    w = env.window()
    fetch(w, comments=False)
    paths = env.archive.paths()
    assert SEARCH in paths and TREE not in paths and IDS not in paths
    s = env.store()
    posts = s.posts("python")
    assert posts and not s.comments("python")
    assert "Added" in w.progress_msg.text() and w.fetch_btn.isEnabled()


def test_removed_posts_are_not_saved(env):
    w = env.window()
    fetch(w, comments=False)
    removed = {p["id"] for p in env.archive.posts if p.get("removed_by_category")}
    saved = {r["id"] for r in env.store().posts("python")}
    assert saved and not saved & removed


def test_comments_come_with_a_fetch_when_asked(env):
    w = env.window()
    fetch(w, comments=True, limit=8)
    s = env.store()
    with_comments = {r["post_id"] for r in s.comments("python")}
    assert with_comments and with_comments <= {r["id"] for r in s.posts("python")}
    assert w.tabbar.tabText(2).replace(" ", " ").split()[-1] != "0"


def test_score_recheck_only_runs_when_ticked(env):
    w = env.window()
    fetch(w, comments=False, limit=40)
    assert any(r["score"] is None for r in env.store().posts("python"))  # recent ones aren't settled
    env.archive.clear()
    fetch(w, comments=False, limit=1, refresh_scores=False)
    assert IDS not in env.archive.paths()
    env.archive.clear()
    fetch(w, comments=False, limit=1, refresh_scores=True)
    assert IDS in env.archive.paths()
    w.close()
    assert env.window().refresh_scores.isChecked()  # remembered


def test_stop_ends_a_fetch_and_frees_the_form(env):
    w = env.window()
    env.archive.delay = 0.15
    w.subreddit.setText("python")
    w.include_comments.setChecked(True)
    w.range.setCurrentIndex(w.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(400)
    assert w.thread is not None and w.stop_btn.isEnabled() and not w.fetch_btn.isEnabled()
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    wait_idle(w)
    env.archive.delay = 0
    n = len(env.archive.paths())
    pump(600)
    assert len(env.archive.paths()) == n  # nothing carries on after Stop
    assert w.fetch_btn.isEnabled() and not w.stop_btn.isEnabled() and w.subreddit.isEnabled()
    assert "Stopped" in w.progress_msg.text() or "stopped" in w.progress_msg.text().lower()


def test_archive_slowdowns_are_waited_out(env):
    w = env.window()
    env.archive.fail(SEARCH, (422, {"error": "Timeout. Maybe slow down a bit"}),
                     (429, {"error": "Too many requests"}), (500, {}))
    fetch(w, comments=False, limit=5)
    assert not env.warnings
    assert len(env.store().posts("python")) == 5


def test_an_archive_that_keeps_failing_shows_one_warning_and_unlocks_the_form(env):
    w = env.window()
    env.archive.fail(SEARCH, *[(400, {"error": "bad request"})] * 3)
    fetch(w, comments=False, limit=5)
    assert len(env.warnings) == 1 and w.fetch_btn.isEnabled()


def test_an_empty_range_says_so(env):
    w = env.window()
    fetch(w, sub="nothinghere", comments=False)
    assert "No new posts" in w.progress_msg.text()
    assert env.store().posts("nothinghere") == []


def test_fetching_another_subreddit_opens_it_and_keeps_the_first(env):
    w = env.window()
    fetch(w, comments=False, limit=5)
    assert w.current.lower() == "python"
    fetch(w, sub="rust", comments=False, limit=5)
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() for i in range(w.datasets.count())]
    assert set(names) == {"python", "rust"}
    assert w.current.lower() == "rust" and len(w.posts) == len(env.store().posts("rust"))


def test_enter_in_the_sidebar_fetches_once(env):
    w = env.window()
    w.subreddit.setText("python")
    w.include_comments.setChecked(False)
    w.subreddit.setFocus()
    QTest.keyClick(w.subreddit, Qt.Key.Key_Return)
    QTest.keyClick(w.subreddit, Qt.Key.Key_Return)  # a second Enter while busy is ignored
    wait_idle(w)
    pump(300)
    assert w.thread is None and env.archive.paths().count(AGG) == 0


def test_switching_subreddit_mid_fetch_keeps_each_table_to_its_own_rows(env):
    w = env.window()
    fetch(w, sub="rust", comments=False, limit=5)
    fetch(w, sub="python", comments=False, limit=2)  # python is open, as when fetching more of it
    env.archive.delay = 0.1
    w.subreddit.setText("python")
    w.include_comments.setChecked(True)
    w.limit.setValue(30)
    w.range.setCurrentIndex(w.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    end = time.monotonic() + 20
    while w.posts_tab.model.rowCount() < 4 and time.monotonic() < end:  # until new rows show up
        pump(20)
    rust = next(i for i in range(w.datasets.count())
                if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "rust")
    w._drip.setInterval(60000)  # hold back the queued rows, as a busy fetch does
    pump(400)
    assert any(w._queued.values()), "some fetched rows should be waiting to fade in"
    w.datasets.setCurrentRow(rust)  # look at another subreddit while python is still downloading
    w._release_rows()
    pump(300)
    shown = {w.posts_tab.model.row_at(i)["subreddit"].lower() for i in range(w.posts_tab.model.rowCount())}
    shown |= {w.comments_tab.model.row_at(i)["subreddit"].lower() for i in range(w.comments_tab.model.rowCount())}
    assert w.thread is not None, "the fetch should still be running"
    assert w.current.lower() == "rust" and shown == {"rust"}
    wait_idle(w)


def test_a_big_download_reads_each_new_row_back_about_once(env, monkeypatch):
    # Every saved row used to re-read all of the fetch's rows so far, which froze the window on a day's
    # Download all (thousands of posts and comments).
    w = env.window()
    fetch(w, comments=False, limit=2)
    read = []
    for name in ("posts_since", "comments_since"):
        original = getattr(w.store, name)
        monkeypatch.setattr(w.store, name, lambda *a, f=original: read.append(len(rows := f(*a))) or rows)
    fetch(w, comments=True, limit=40, rng="month")
    s = env.store()
    saved = len(s.posts("python")) + len(s.comments("python"))
    assert saved > 60 and sum(read) < 4 * saved  # linear; it was about saved² / 2


def test_a_busy_subreddit_that_times_out_still_fetches_with_narrower_queries(env):
    w = env.window()
    fetch(w, sub="askreddit", comments=False, limit=30, rng="day")
    timeouts = [r for r in env.archive.rejected if r[2] == 422]
    assert timeouts and not env.warnings  # it hit the archive's timeouts and worked around them
    assert len(env.store().posts("askreddit")) >= 20


def test_a_keyword_search_the_archive_cant_run_says_so_once(env):
    w = env.window()
    w.query.setText("black holes")
    fetch(w, sub="askreddit", comments=False, limit=10, rng="day")
    assert len(env.warnings) == 1 and "timed out" in env.warnings[0].lower()
    assert w.fetch_btn.isEnabled()


def test_a_fetch_at_the_apps_real_pace_never_trips_the_rate_limit(env, monkeypatch):
    # The real limiter (a request every 0.4 s) against a server allowing 3 a second.
    monkeypatch.setattr(worker.LIMITER, "base", arctic_shift.BASE_INTERVAL)
    monkeypatch.setattr(worker.LIMITER, "interval", arctic_shift.BASE_INTERVAL)
    env.archive.rate = (3, 1.0)
    w = env.window()
    fetch(w, comments=True, limit=4, rng="day")
    assert [r for r in env.archive.rejected if r[2] == 429] == []
    assert len(env.archive.paths()) >= 4


def test_when_the_server_says_no_requests_are_left_the_app_waits_for_the_reset(env):
    w = env.window()
    env.archive.rate = (2, 1.0)  # far below the test pace
    fetch(w, comments=True, limit=4, rng="day")
    assert [r for r in env.archive.rejected if r[2] == 429] == []  # it waited instead of hitting the limit
    assert not env.warnings and len(env.store().posts("python")) == 4
