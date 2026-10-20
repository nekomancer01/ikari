"""The whole app driven like a user would: the real main window, fetch threads, HTTP client and store,
talking to a fake Arctic Shift server on localhost. The rule checked throughout: nothing is requested
unless the user asked for it, and each action requests only what it says."""
import os
import sqlite3
import subprocess
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QSize, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from subsentiment import arctic_shift  # noqa: E402
from subsentiment.gui import coverage  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import worker  # noqa: E402
from subsentiment.store import Store  # noqa: E402

from fake_archive import DAY, FakeArchive, make_posts  # noqa: E402

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
    # A new limiter for each new server: the shared one remembers the last test's request budget.
    monkeypatch.setattr(worker, "LIMITER", arctic_shift.RateLimiter(arctic_shift.BASE_INTERVAL / 10))
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
            stores.append(Store(self.db))
            return stores[-1]
    stores = []
    yield Env()
    for w in windows:
        w.close()
    for s in stores:
        s.close()
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
    stale = {r["id"] for r in env.store().posts("python") if r["score"] is None}
    assert stale  # recent ones aren't settled

    def rechecked():
        return {i for p, q in env.archive.requests if p == IDS for i in q["ids"].split(",")} & stale
    env.archive.clear()
    fetch(w, comments=False, limit=1, refresh_scores=False)
    assert not rechecked()
    env.archive.clear()
    fetch(w, comments=False, limit=1, refresh_scores=True)
    assert rechecked() == stale
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


def test_stop_takes_effect_at_once_even_while_requests_wait_out_timeouts(env, monkeypatch):
    # Real waits (a timeout costs 2 s, then 4 s): r/AskReddit times out on most pages, so four requests
    # are sitting in backoffs when Stop is pressed.
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 120.0)
    w = env.window()
    w.subreddit.setText("AskReddit")
    w.include_comments.setChecked(True)
    w.range.setCurrentIndex(w.range.findData("day"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(1500)
    assert w.thread is not None
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    stopped_at, seen = time.monotonic(), set()
    while w.thread is not None:
        assert time.monotonic() - stopped_at < 3, f"still stopping; it said {seen}"
        seen.add(w.progress_msg.text())
        QApplication.processEvents()
        time.sleep(0.01)
    # Not overwritten by "Waiting 4 s…" from the requests winding down; then the result.
    assert "Stopping…" in seen and all(t.startswith("Stopp") for t in seen), seen
    assert not env.warnings


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


@pytest.mark.parametrize("saved_before", [False, True])
def test_fetching_another_subreddit_shows_it_as_it_downloads(env, saved_before):
    # The window used to stay on whatever was open for the whole fetch, so nothing seemed to happen.
    w = env.window()
    if saved_before:
        fetch(w, comments=False, limit=2)
    fetch(w, sub="rust", comments=False, limit=5)
    assert w.current.lower() == "rust"
    env.archive.delay = 0.1
    w.subreddit.setText("python")
    w.include_comments.setChecked(True)
    w.limit.setValue(30)
    w.range.setCurrentIndex(w.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    s = env.store()
    end = time.monotonic() + 20
    while len(s.posts("python")) < 4 + 2 * saved_before and time.monotonic() < end:
        pump(20)
    pump(300)  # a few fade-in ticks, well under the 1 s refresh
    assert w.thread is not None, "the fetch should still be running"
    assert w.current.lower() == "python"
    assert w.posts_tab.model.rowCount() > 2 * saved_before
    wait_idle(w)


def test_closing_mid_fetch_leaves_the_fetch_alone(env, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *e: errors.append(e))
    w = env.window()
    env.archive.delay = 4  # a request still out when the window closes
    w.subreddit.setText("python")
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(500)
    w.close()
    assert w.running_threads(), "app.main ends the process instead of destroying this thread"
    end = time.monotonic() + 20
    while w.thread.isRunning() and time.monotonic() < end:
        pump(50)
    pump(200)
    assert errors == []  # the fetch ending didn't reach for the closed store


QUIT_MID_FETCH = """
import os, sys
from PySide6.QtCore import QSettings, QTimer
from subsentiment import arctic_shift
from subsentiment.gui import app, main_window, worker
arctic_shift.BASE_URL = sys.argv[1]
worker.LIMITER.base = worker.LIMITER.interval = arctic_shift.BASE_INTERVAL / 10  # as in the other tests
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, sys.argv[2])
QSettings(app.STORAGE_ORG, app.STORAGE_APP).setValue("updates/auto", False)
app.default_db_path = lambda: os.path.join(sys.argv[2], "quit.db")
def window(path):
    w = main_window.MainWindow(path)
    w.subreddit.setText("python")
    w.include_comments.setChecked(True)
    w.limit.setValue(10)
    if sys.argv[3] == "top":
        w.pick.setCurrentIndex(w.pick.findData("top"))
    def close():
        print("saved", len(w.store.posts("python")) + len(w.store.comments("python")), flush=True)
        w.close()  # while a slow request for comments is still out
        print("running", bool(w.running_threads()), flush=True)
    def close_once_posts_are_saved():
        if sys.argv[3] == "idle":
            close()
        elif sys.argv[3] == "top":
            QTimer.singleShot(1500, close)  # the day's candidates are ranked; the download of the picks is out
        elif len(w.store.posts("python")) >= 5:
            QTimer.singleShot(1500, close)  # the rest of the posts, then the comments start
        else:
            QTimer.singleShot(50, close_once_posts_are_saved)
    if sys.argv[3] != "idle":
        QTimer.singleShot(300, w.fetch_btn.click)
    QTimer.singleShot(400, close_once_posts_are_saved)
    return w
app.MainWindow = window
sys.exit(app.main([]))
"""


# fetch: a comment request is still out at quit. Stop doesn't wait for comment trees, so the fetch ends
#   when the window closes, and the request left out must not hold up the process.
# top: Top by score, quitting while the download of a day's ranked picks is out. Stop lets that request
#   finish, so the fetch is still running when the process ends.
# idle: quitting with nothing running, for comparison.
@pytest.mark.parametrize("mode", ["fetch", "top", "idle"])
def test_quitting_mid_fetch_exits_cleanly(env, tmp_path, mode):
    # Qt aborts the process (an access violation on Windows) when a thread is destroyed while running.
    # Before, the process also lived on with the fetch after the window had gone, raising in its signals.
    env.archive.delays[TREE] = 20  # posts come quickly, then a comment request is still out at quit
    env.archive.delays[IDS] = 20
    start = time.monotonic()
    run = subprocess.run([sys.executable, "-X", "faulthandler", "-c", QUIT_MID_FETCH, env.archive.url,
                          str(tmp_path), mode],
                         capture_output=True, text=True, timeout=90,
                         env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
    assert run.returncode == 0, run.stderr[-2000:]
    assert "Traceback" not in run.stderr and "Destroyed while thread" not in run.stderr, run.stderr[-2000:]
    out = dict(line.split() for line in run.stdout.split("\n") if line.startswith(("saved", "running")))
    assert out["running"] == str(mode == "top")  # the fetch was still going when the process ended
    assert time.monotonic() - start < 20  # gone without waiting for it
    # Ending the process skips every clean-up, so check nothing is lost: each row saved before the
    # window closed is in the database, which is intact, and the form's settings were written.
    db = tmp_path / "quit.db"
    with sqlite3.connect(db) as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    s = Store(str(db))
    assert len(s.posts("python")) + len(s.comments("python")) >= int(out["saved"])
    s.close()
    assert not (tmp_path / "quit.db-wal").exists() or (tmp_path / "quit.db-wal").stat().st_size == 0
    settings = QSettings(str(next(tmp_path.rglob("*.ini"))), QSettings.Format.IniFormat)
    assert settings.value("fetch/subreddit") == "python"


def test_about_names_the_bundled_model(env, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "about", lambda parent, title, text: shown.append(text))
    env.window()._show_about()
    assert shown and "VADER" not in shown[0]


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
    while not any(w._queued.values()) and time.monotonic() < end:
        pump(20)
    assert any(w._queued.values()), "some fetched rows should be waiting to fade in"
    w.datasets.setCurrentRow(rust)  # look at another subreddit while python is still downloading
    w._release_rows()
    pump(300)
    shown = {w.posts_tab.model.row_at(i)["subreddit"].lower() for i in range(w.posts_tab.model.rowCount())}
    shown |= {w.comments_tab.model.row_at(i)["subreddit"].lower() for i in range(w.comments_tab.model.rowCount())}
    assert w.thread is not None, "the fetch should still be running"
    assert w.current.lower() == "rust" and shown == {"rust"}
    wait_idle(w)


def test_rows_keep_fading_in_while_comments_are_saved_quickly(env):
    # Each save used to restart the fade-in timer, so while comments were saved faster than it ticks,
    # no queued post appeared until the fetch ended.
    w = env.window()
    fetch(w, comments=False, limit=12)
    n = len(w.posts)
    w.posts_tab.load([])
    w.request, w.thread = mw.collect.FetchRequest("python"), object()  # as if python were being fetched
    w._read_to = {"posts": 0.0, "comments": 0.0}
    end = time.monotonic() + 1.5
    while w.posts_tab.model.rowCount() < n and time.monotonic() < end:
        w._on_saved()  # a comment saved every 10 ms
        pump(10)
    w._live.stop()
    w.thread = None
    assert w.posts_tab.model.rowCount() == n


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


def test_a_busy_subreddit_that_times_out_still_fetches_with_narrower_queries(env, monkeypatch):
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.3)  # timeout back-offs, shortened further
    w = env.window()
    fetch(w, sub="askreddit", comments=False, limit=30, rng="day")
    timeouts = [r for r in env.archive.rejected if r[2] == 422]
    assert timeouts and not env.warnings  # it hit the archive's timeouts and worked around them
    assert len(env.store().posts("askreddit")) >= 20


def test_a_keyword_search_the_archive_cant_run_says_so_once(env, monkeypatch):
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.3)  # timeout back-offs, shortened further
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


def open_coverage(w):
    w.coverage_act.trigger()
    end = time.monotonic() + 30
    while (w.coverage._count_thread is not None or not w.coverage.days) and time.monotonic() < end:
        pump(20)
    pump(50)
    return w.coverage


def test_coverage_counts_a_busy_subreddit_without_downloading_anything(env, monkeypatch):
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.3)  # timeout back-offs, shortened further
    w = env.window()
    fetch(w, sub="askreddit", comments=False, limit=5, rng="day")
    env.archive.clear()
    w.range.setCurrentIndex(w.range.findData("month"))
    cov = open_coverage(w)
    paths = set(env.archive.paths())
    assert paths == {AGG}  # only counts; the month-long count timed out and was split, never refused
    assert [r for r in env.archive.rejected if r[2] == 422]
    totals = cov.totals["askreddit"]
    assert sum(totals.values()) == len([p for p in env.archive.posts if p["subreddit"] == "AskReddit"
                                        and p["created_utc"] >= w.fetch_range()[0]])
    for row in range(min(5, cov.day_table.rowCount())):  # browsing days and posts
        cov.day_table.selectRow(row)
        pump(30)
    QTest.keyClick(cov.day_table, Qt.Key.Key_Return)
    QTest.keyClick(cov.post_table, Qt.Key.Key_Return)
    pump(500)
    assert set(env.archive.paths()) == {AGG} and w.thread is None


def test_coverage_counts_posts_removed_from_reddit_as_checked(env):
    # Every fifth post in the fake archive was removed. A day's count includes them, so once Download all
    # has been through a day, it reads complete, and before that its share counts the removed ones seen.
    w = env.window()
    fetch(w, comments=False, limit=40)
    cov = open_coverage(w)
    removed = w.store.removed_per_day("python", cov.days[-1], cov.days[0] + DAY)
    assert sum(removed.values()) > 0
    row, day = next((r, d) for r, d in enumerate(cov.days) if removed.get(d) and not no_total_day(cov, d))
    cell = cov.day_table.cellWidget(row, 1)
    saved = w.store.posts_per_day("python", day, day + DAY)[day]
    total = cov.totals["python"][day]
    assert cell.main.text() == f"{saved} saved"
    assert cell.side.text() == f"{100 * min(1, (saved + removed[day]) / total):.0f}%"
    assert f"{removed[day]} removed from Reddit" in cell.toolTip()
    QTest.mouseClick(cov.day_table.cellWidget(row, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    wait_idle(w)
    pump(100)
    assert cov.day_table.cellWidget(row, 1).side.text() == "✓ Complete"


def no_total_day(cov, day):
    return day >= coverage.today()


def busy_day(env, sub: str, n: int) -> int:
    """Adds ``n`` posts to the archive on the day three days back, and returns that day."""
    day = int((NOW - 3 * DAY) // DAY * DAY)
    base = make_posts(sub, NOW, days=1, per_day=(1,), seed=4)[0]
    env.archive.posts += [dict(base, id=f"bd{i:05d}", title=f"question {i}", created_utc=day + 30 + i * 50,
                               num_comments=3, permalink=f"/r/{sub}/comments/bd{i:05d}/x/") for i in range(n)]
    return day


def wait_idle_stalls(w, timeout: float = 60) -> float:
    """wait_idle, returning the longest the window went without handling events (a freeze)."""
    end, worst = time.monotonic() + timeout, 0.0
    while w.thread is not None:
        assert time.monotonic() < end, "fetch didn't finish"
        t = time.perf_counter()
        QApplication.processEvents()
        worst = max(worst, time.perf_counter() - t)
        time.sleep(0.01)
    for _ in range(20):
        t = time.perf_counter()
        QApplication.processEvents()
        worst = max(worst, time.perf_counter() - t)
        time.sleep(0.01)
    return worst


def test_coverage_stays_responsive_while_a_busy_day_downloads(env):
    # A day with 1,500 posts used to rebuild 4,500 widgets on every refresh: 2-3 s frozen each time.
    day = busy_day(env, "bigday", 1500)
    w = env.window()
    fetch(w, sub="bigday", comments=False, limit=3)
    cov = open_coverage(w)
    row = cov.days.index(day)
    cov.day_table.selectRow(row)
    pump(100)
    env.archive.delay = 0.02
    QTest.mouseClick(cov.day_table.cellWidget(row, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    worst = wait_idle_stalls(w)
    assert worst < 1.0
    t = cov.post_table
    assert t.rowCount() == 1500 and t.cellWidget(1499, 1) is None  # rows off screen aren't built
    t.scrollToBottom()
    pump(50)
    assert t.cellWidget(1499, 0).toolTip() or t.cellWidget(1499, 0).text()  # scrolled to: built and filled
    assert t.cellWidget(1499, 2).findChild(type(w.fetch_btn)).isEnabled()


def test_coverage_downloads_do_only_what_they_say(env):
    w = env.window()
    fetch(w, comments=False, limit=3)
    cov = open_coverage(w)
    day_row = next(r for r, d in enumerate(cov.days) if cov.totals["python"].get(d))
    cov.day_table.selectRow(day_row)
    env.archive.clear()
    QTest.mouseClick(cov.day_table.cellWidget(day_row, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    wait_idle(w)
    assert set(env.archive.paths()) <= {SEARCH, IDS, AGG}  # the day's posts, no comments
    pump(100)
    env.archive.clear()
    button = cov.post_table.cellWidget(0, 2).findChild(type(w.fetch_btn))
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)
    wait_idle(w)
    trees = [q for p, q in env.archive.requests if p == TREE]
    assert len(trees) == 1 and set(env.archive.paths()) <= {TREE, IDS}


def test_coverage_keeps_its_place_while_a_download_runs(env):
    w = env.window()
    fetch(w, comments=False, limit=3)
    w.range.setCurrentIndex(w.range.findData("month"))
    cov = open_coverage(w)
    cov.resize(900, 400)
    pump(50)
    last = cov.day_table.rowCount() - 1
    cov.day_table.selectRow(last)
    cov.day_table.scrollToBottom()
    pump(50)
    scrolled = cov.day_table.verticalScrollBar().value()
    assert scrolled > 0
    env.archive.delay = 0.05
    w.subreddit.setText("python")
    w.include_comments.setChecked(True)
    w.limit.setValue(20)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(2500)  # the window refreshes about once a second while rows arrive
    assert cov.day_table.verticalScrollBar().value() == scrolled
    assert cov._selected_day() == cov.days[last]
    wait_idle(w)


def test_coverage_counts_a_subreddit_picked_while_another_is_being_counted(env):
    # The first count's end used to be taken for days that couldn't be counted: the new subreddit was
    # never counted and every day read "total unknown", with no message, until Recount.
    w = env.window()
    fetch(w, comments=False, limit=20)
    fetch(w, sub="rust", comments=False, limit=20)
    w.datasets.setCurrentRow(next(i for i in range(w.datasets.count())
                                  if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "python"))
    env.archive.delays[AGG] = 1.0
    w.coverage_act.trigger()
    pump(200)
    assert w.coverage._count_thread is not None  # python's count is still out
    w.datasets.setCurrentRow(next(i for i in range(w.datasets.count())
                                  if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "rust"))
    end = time.monotonic() + 15
    while ("rust" not in w.coverage.totals or w.coverage._count_thread is not None) and time.monotonic() < end:
        pump(20)
    cov = w.coverage
    assert cov.subreddit.lower() == "rust" and cov.count_error == ""
    assert all(d in cov.totals["rust"] for d in cov.days)


def test_coverage_follows_a_change_of_date_range_in_the_sidebar(env):
    w = env.window()
    fetch(w, comments=False, limit=20)
    cov = open_coverage(w)
    assert len(cov.days) == 8
    w.range.setCurrentIndex(w.range.findData("month"))
    pump(500)
    assert len(cov.days) == 31 and cov.day_table.rowCount() == 31
    w.date_from.setDate(w.date_to.date().addDays(-2))  # typing a date: a custom range
    pump(500)
    assert len(cov.days) == 3


def test_closing_coverage_while_a_count_is_out_doesnt_freeze_the_window(env):
    w = env.window()
    fetch(w, comments=False, limit=20)
    env.archive.delays[AGG] = 3
    w.coverage_act.trigger()
    pump(200)
    assert w.coverage._count_thread is not None
    start = time.monotonic()
    w.coverage.close()
    assert time.monotonic() - start < 0.5  # it used to wait up to 3 s for the request
    end = time.monotonic() + 10
    while w.coverage._count_thread is not None and time.monotonic() < end:
        pump(20)
    assert w.coverage._count_thread is None  # the count still ends and is tidied up


def test_a_fetch_ending_leaves_the_user_on_the_subreddit_they_switched_to(env):
    w = env.window()
    fetch(w, sub="rust", comments=False, limit=20)
    fetch(w, comments=False, limit=3)
    env.archive.delay = 0.15
    w.subreddit.setText("python")
    w.limit.setValue(40)
    w.range.setCurrentIndex(w.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(600)
    w.datasets.setCurrentRow(next(i for i in range(w.datasets.count())
                                  if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "rust"))
    pump(50)
    assert w.thread is not None and w.current.lower() == "rust"
    wait_idle(w, 60)
    assert w.current.lower() == "rust" and w.datasets.currentItem().text().lower() == "r/rust"
    assert "Added" in w.progress_msg.text()


def test_stop_in_the_coverage_window_is_disabled_once_pressed(env):
    w = env.window()
    fetch(w, comments=False, limit=20)
    cov = open_coverage(w)
    env.archive.delay = 0.15
    QTest.mouseClick(cov.day_table.cellWidget(1, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    pump(300)
    assert cov.stop_btn.isEnabled()
    QTest.mouseClick(cov.stop_btn, Qt.MouseButton.LeftButton)
    assert not cov.stop_btn.isEnabled() and not w.stop_btn.isEnabled()
    w.coverage_act.trigger()  # bringing the window forward again while it stops
    assert not cov.stop_btn.isEnabled()
    wait_idle(w)


def test_coverage_closed_and_opened_again_mid_count_counts_again_without_an_error(env):
    w = env.window()
    fetch(w, comments=False, limit=20)
    env.archive.delays[AGG] = 1.5
    w.coverage_act.trigger()
    pump(200)
    w.coverage.close()  # stops the count; its answer comes back cut short
    w.coverage_act.trigger()
    end = time.monotonic() + 15
    while ("python" not in w.coverage.totals or w.coverage._count_thread is not None) and time.monotonic() < end:
        pump(20)
    pump(100)
    cov = w.coverage
    assert cov.count_error == "" and all(d in cov.totals["python"] for d in cov.days)


def test_choosing_days_never_downloads_and_a_fetch_only_adds_rows_from_chosen_days(env):
    w = env.window()
    fetch(w, comments=False, limit=10)
    saved = w.store.day_counts("python")
    day = max(d for d, (posts, _) in saved.items() if posts)
    env.archive.clear()

    w.days_pick.showPopup()  # open the picker, keep only one day, close it
    pump(50)
    w.days_pick.picker.none_btn.click()
    cal = w.days_pick.picker.calendar
    QTest.mouseClick(cal, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, cal.rect_of(day).center())
    pump(300)  # longer than the picker's settling delay
    w.days_pick.hidePopup()
    for i in range(3):
        w.tabbar.setCurrentIndex(i)
        pump(30)
    pump(1200)
    assert env.archive.paths() == []  # picking days only filters what's saved
    assert w.chosen_days == {day} and len(w.posts) == saved[day][0]

    env.archive.delay = 0.05
    w.subreddit.setText("python")
    w.limit.setValue(60)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    seen = 0
    while w.thread is not None:  # rows from other days are saved but not shown while the fetch runs
        QApplication.processEvents()
        rows = [w.posts_tab.model.row_at(i) for i in range(w.posts_tab.model.rowCount())]
        assert all(int(r["created_utc"] // DAY * DAY) == day for r in rows)
        seen = max(seen, len(rows))
        time.sleep(0.01)
    pump(50)
    env.archive.delay = 0
    on_day = [r for r in env.store().posts("python") if int(r["created_utc"] // DAY * DAY) == day]
    assert w.posts_tab.model.rowCount() == len(on_day) == len(w.posts)
    assert len(w.days_pick.counts) > len(saved) or sum(p for p, _ in w.days_pick.counts.values()) > \
        sum(p for p, _ in saved.values())  # the picker knows about what the fetch added
    assert w.days_pick.currentText() != "All days"


def test_a_second_fetch_into_a_shown_subreddit_ends_without_reading_it_all_back(env, monkeypatch):
    w = env.window()
    fetch(w, limit=5)
    full_reads = []
    for name in ("posts", "comments"):
        real = getattr(Store, name)
        monkeypatch.setattr(Store, name, lambda self, *a, real=real, name=name: (
            full_reads.append(name) if self is w.store else None, real(self, *a))[1])
    fetch(w, limit=100, refresh_scores=True)  # more posts and comments, and saved ones re-checked
    assert full_reads == []  # it used to read every saved row again as the fetch ended
    store = env.store()
    for tab, saved in ((w.posts_tab, store.posts("python")), (w.comments_tab, store.comments("python"))):
        assert tab.model.rowCount() == len(saved) == len(tab.all_rows)
        shown = {tab.model.row_at(i)["id"]: tuple(tab.model.row_at(i)) for i in range(tab.model.rowCount())}
        assert shown == {r["id"]: tuple(r) for r in saved}  # new scores included
    assert w.tiles["count"].value.text() == f"{len(store.posts('python')) + len(store.comments('python')):,}"
