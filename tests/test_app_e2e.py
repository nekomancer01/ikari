"""The whole app driven like a user would: the real main window, fetch threads, HTTP client and store,
talking to a fake Arctic Shift server on localhost. The rule checked throughout: nothing is requested
unless the user asked for it, and each action requests only what it says."""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, QSize, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton  # noqa: E402

from subsentiment import arctic_shift  # noqa: E402
from subsentiment.gui import coverage, days  # noqa: E402
from subsentiment.gui import tables  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import worker  # noqa: E402
from subsentiment.gui.rail import PAGES  # noqa: E402
from subsentiment.store import Store  # noqa: E402
from subsentiment import store as store_mod  # noqa: E402

from fake_archive import DAY, SLOW_DOWN, FakeArchive, make_posts  # noqa: E402

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
    while w.fetch_job.busy:
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


def in_range(env, w, sub="Python") -> int:
    """The posts the archive has for ``sub`` in the sidebar's range that a fetch can save (not removed)."""
    after, before = w.fetch_form.bounds()
    return len([p for p in env.archive.posts if p["subreddit"] == sub and after <= p["created_utc"] < before
                and not p.get("removed_by_category")])


def fetch(w, sub="python", comments=True, rng="week", timeout: float = 30, **extra):
    """A fetch from the Fetch drawer, opened from the rail, which downloads every post in the range."""
    if w._drawer_key() != "fetch":
        QTest.mouseClick(w.rail.buttons["fetch"], Qt.MouseButton.LeftButton)
    assert w._drawer_key() == "fetch"
    w.fetch_form.subreddit.setText(sub)
    w.fetch_form.include_comments.setChecked(comments)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData(rng))
    for key, value in extra.items():
        getattr(w.fetch_form, key).setChecked(value)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    wait_idle(w, timeout)


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
    for page in PAGES:  # every page but Download coverage, and every Overview scope
        if page != "coverage":
            w.show_page(page)
    for i in range(w.scope.count()):
        w.scope.setCurrentIndex(i)
    for size in (QSize(1280, 750), QSize(900, 600), QSize(1900, 1100)):
        w.resize(size)
        pump(30)
    w.showMaximized()
    pump(1200)  # longer than the redraw timer's 1 s
    assert env.archive.paths() == []
    w.show_page("coverage")  # asks the archive how many posts each day has, and nothing else
    pump(1200)
    assert set(env.archive.paths()) == {AGG}
    assert not w.fetch_job.busy


def test_the_fetch_drawer_closes_as_a_fetch_starts_and_the_header_follows_it(env):
    w = env.window()
    assert w._drawer_key() == "fetch" and w.fetch_form.subreddit.hasFocus()  # nothing saved: it starts open
    QTest.mouseClick(w.rail.buttons["fetch"], Qt.MouseButton.LeftButton)  # the rail closes it again
    assert w.drawer.isHidden() and not w.rail.buttons["fetch"].isChecked()
    QTest.mouseClick(w.rail.buttons["fetch"], Qt.MouseButton.LeftButton)
    env.archive.delay = 0.02
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(False)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    assert w.fetch_job.busy and w.drawer.isHidden()
    # Progress, what it's doing and Stop show under the header on every page, and the rail marks Fetch busy.
    for page in PAGES:
        QTest.mouseClick(w.rail.buttons[page], Qt.MouseButton.LeftButton)
        assert w.page() == page and w.pages.currentWidget() is w.page_widgets[page]
        assert w.progress.isVisible() and w.stop_btn.isVisible() and w.stop_btn.isEnabled()
    assert w.rail.buttons["fetch"].busy
    wait_idle(w)
    assert not w.rail.buttons["fetch"].busy and not w.progress.isVisible() and not w.stop_btn.isVisible()
    assert "Added" in w.progress_msg.text() and w.fetch_strip.isVisible()
    QTest.mouseClick(w.fetch_strip.hide_btn, Qt.MouseButton.LeftButton)
    assert not w.fetch_strip.isVisible()


def test_the_strip_keeps_saying_what_the_fetch_is_doing_while_a_search_waits_out_timeouts(env, monkeypatch):
    # The archive times a search out twice, then answers slowly. Each timeout used to hold back every request
    # and put "Arctic Shift is slow right now. Waiting 4 s…" in the strip, over and over, while nothing saved.
    # Now only the search that timed out waits, so the strip goes on saying which day is downloading.
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 5.0)
    w = env.window()
    env.archive.fail(SEARCH, *[SLOW_DOWN] * 8)
    env.archive.delays[SEARCH] = 1.5
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(False)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("day"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    end = time.monotonic() + 30
    said = set()
    while len(env.archive.faults[SEARCH]) > 0 or len(env.archive.paths()) < 10:  # the timeouts, then answers
        assert w.fetch_job.busy and time.monotonic() < end, "the timeouts were never waited out"
        said.add(w.progress_msg.text())
        pump(5)
    pump(300)  # a search asked again is still out (1.5 s)
    said.add(w.progress_msg.text())
    assert not [m for m in said if "Waiting" in m or "slow" in m], said
    assert w.progress_msg.text().startswith("Day "), w.progress_msg.text()
    env.archive.delays.clear()
    wait_idle(w)
    assert "Added" in w.progress_msg.text()


def test_a_fetch_without_comments_asks_for_no_comments(env):
    w = env.window()
    fetch(w, comments=False)
    paths = env.archive.paths()
    assert SEARCH in paths and TREE not in paths and IDS not in paths
    s = env.store()
    posts = s.posts("python")
    assert posts and not s.comments("python")
    assert "Added" in w.progress_msg.text() and w.fetch_btn.isEnabled()


def test_a_fetch_downloads_every_post_in_the_range_a_day_at_a_time(env):
    w = env.window()
    labels = [lbl.text() for lbl in w.findChildren(QLabel)]
    assert "Max posts" not in labels and "Pick" not in labels  # nothing to choose: it's the whole range
    env.archive.delay = 0.02
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(False)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    bars, lines = set(), set()
    shown = w.progress_msg.setText  # every line the strip is given: a short day's can go before a look sees it
    w.progress_msg.setText = lambda text: (lines.add(text), shown(text))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    while w.fetch_job.busy:
        bars.add((w.progress.value(), w.progress.maximum()))
        lines.add(w.progress_msg.text())
        pump(5)
    pump(50)
    assert len(env.store().posts("python")) == in_range(env, w) > 30
    # The bar counts the range's UTC days (eight for a week that doesn't start at midnight), newest first.
    assert {total for _, total in bars} >= {8} and any(t.startswith("Day 1 of 8") for t in lines), (bars, lines)
    after = w.fetch_form.bounds()[0]
    whole = {str(d) for d in range(int(after // DAY * DAY) + DAY, coverage.today(), DAY)}
    assert env.store().complete_keys("posts", "python") == whole  # Download coverage shows them as done
    env.archive.clear()
    fetch(w, comments=False)  # again: the complete days aren't asked for, only the two partial ones
    windows = {(int(q["after"]) + 1) // DAY for p, q in env.archive.requests if p == SEARCH}
    assert windows <= {int(after // DAY), int(coverage.today() // DAY)}


def test_a_long_fetch_shows_comments_from_its_first_day_on(env):
    # Live: a week of r/AskReddit read every post before any comment, so the first minutes showed none.
    w = env.window()
    env.archive.delay = 0.01
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    s = env.store()
    posts_at_first_comment = None
    while w.fetch_job.busy:
        if posts_at_first_comment is None and s.comments("python"):
            posts_at_first_comment = len(s.posts("python"))
        pump(5)
    pump(50)
    total = len(s.posts("python"))
    assert s.comments("python") and posts_at_first_comment is not None
    assert posts_at_first_comment < total / 2, (posts_at_first_comment, total)  # the newest day's, not all


def test_removed_posts_are_not_saved(env):
    w = env.window()
    fetch(w, comments=False)
    removed = {p["id"] for p in env.archive.posts if p.get("removed_by_category")}
    saved = {r["id"] for r in env.store().posts("python")}
    assert saved and not saved & removed


def test_image_posts_are_skipped_hidden_and_count_as_checked(env):
    # Some of the archive's posts are pictures: they aren't shown, scored, exported or given comments, but the
    # archive's day counts include them, so coverage counts them as checked and a day can still read complete.
    w = env.window()
    after, before = w.fetch_form.bounds()
    pics = [p for p in env.archive.posts if p["subreddit"] == "Python" and after <= p["created_utc"] < before
            and not p.get("removed_by_category")][::3]
    for p in pics:
        p.update(url="https://i.redd.it/x.jpeg", selftext="", post_hint="image")
    with_words = pics[0]
    with_words["selftext"] = "my desk, details below"  # a picture with real text stays
    ids = {p["id"] for p in pics[1:]}
    fetch(w, comments=True)
    s = env.store()
    saved = {r["id"] for r in s.posts("python")}
    assert saved and not saved & ids and with_words["id"] in saved
    assert not [c for c in s.comments("python") if c["post_id"] in ids]
    assert w.store.datasets()[0]["posts"] == len(saved)
    csv_path = os.path.join(os.path.dirname(env.db), "p.csv")
    s.export_csv("python", "posts", csv_path)
    assert not any(i in open(csv_path, encoding="utf-8-sig").read() for i in ids)
    cov = open_coverage(w)
    skipped = w.store.posts_per_day("python", cov.days[-1], cov.days[0] + DAY, images=True)
    assert sum(skipped.values()) == len(ids)
    assert all(cov.day_table.cellWidget(r, 1).side.text() == "✓ Complete"
               for r, d in enumerate(cov.days) if d >= w.fetch_form.bounds()[0] and d < coverage.today())
    fetch(w, comments=False)  # a repeat fetch doesn't save them either
    assert not {r["id"] for r in env.store().posts("python")} & ids


def test_all_comments_fetches_each_whole_thread_and_is_remembered(env):
    w = env.window()
    w.fetch_form.comments_per_post.setValue(0)
    assert w.fetch_form.comments_per_post.text() == "All"
    w.fetch_form.comments_per_post.setValue(50)
    for _ in range(5):  # 40, 30, 20, 10, then All
        QTest.keyClick(w.fetch_form.comments_per_post, Qt.Key.Key_Down)
    assert w.fetch_form.comments_per_post.value() == 0 and w.fetch_form.comments_per_post.text() == "All"  # spinning down past 1
    fetch(w, comments=True)
    s = env.store()
    posts = {r["id"]: r for r in s.posts("python")}
    trees = [q for p, q in env.archive.requests if p == TREE]
    # Every thread asked for in full (its known size plus room), not the top 50.
    assert trees and all(int(q["limit"]) >= 100 for q in trees)
    settled = {pid for pid, r in posts.items() if r["score"] is not None and r["num_comments"]}
    assert settled and s.complete_keys("comments", "python") == settled  # Download coverage shows them done
    env.archive.clear()
    fetch(w, comments=True)
    asked = {q["link_id"] for p, q in env.archive.requests if p == TREE}
    assert asked and not asked & settled  # only the threads that can still grow are asked again
    w.close()
    assert env.window().fetch_form.comments_per_post.value() == 0  # remembered


def test_comments_come_with_a_fetch_when_asked(env):
    w = env.window()
    fetch(w, comments=True)
    s = env.store()
    with_comments = {r["post_id"] for r in s.comments("python")}
    assert with_comments and with_comments <= {r["id"] for r in s.posts("python")}
    assert " 0 comments" not in w.subheading.full_text() and "comment" in w.subheading.full_text()


def test_score_recheck_only_runs_when_ticked(env):
    w = env.window()
    fetch(w, comments=False)
    stale = {r["id"] for r in env.store().posts("python") if r["score"] is None}
    assert stale  # recent ones aren't settled

    def rechecked():
        return {i for p, q in env.archive.requests if p == IDS for i in q["ids"].split(",")} & stale
    env.archive.clear()
    fetch(w, comments=False, refresh_scores=False)
    assert not rechecked()
    env.archive.clear()
    fetch(w, comments=False, refresh_scores=True)
    assert rechecked() == stale
    w.close()
    assert env.window().fetch_form.refresh_scores.isChecked()  # remembered


def test_stop_ends_a_fetch_and_frees_the_form(env, monkeypatch):
    sent = []  # every request the limiter let out
    let_out = worker.LIMITER.wait

    def counted(*a, **k):
        sent.append(let_out(*a, **k))
        return sent[-1]
    monkeypatch.setattr(worker.LIMITER, "wait", counted)
    w = env.window()
    env.archive.delay = 0.15
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(400)
    assert w.fetch_job.busy and w.stop_btn.isEnabled() and not w.fetch_btn.isEnabled()
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    wait_idle(w)
    env.archive.delay = 0
    # Requests let out before Stop can still be on their way: on a loaded runner one reached the archive
    # after this point. Counted once every request sent has arrived.
    end = time.monotonic() + 10
    while len(env.archive.paths()) < len(sent) and time.monotonic() < end:
        pump(20)
    n = len(env.archive.paths())
    pump(600)
    assert len(env.archive.paths()) == n == len(sent)  # nothing carries on after Stop
    assert w.fetch_btn.isEnabled() and not w.stop_btn.isEnabled() and w.fetch_form.subreddit.isEnabled()
    assert "Stopped" in w.progress_msg.text() or "stopped" in w.progress_msg.text().lower()


def test_stop_takes_effect_at_once_even_while_requests_wait_out_timeouts(env, monkeypatch):
    # Real waits (a timeout costs 2 s, then 4 s): r/AskReddit times out on most pages, so four requests
    # are sitting in backoffs when Stop is pressed.
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 120.0)
    w = env.window()
    w.fetch_form.subreddit.setText("AskReddit")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("day"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(1500)
    assert w.fetch_job.busy
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    stopped_at, seen = time.monotonic(), set()
    while w.fetch_job.busy:
        assert time.monotonic() - stopped_at < 3, f"still stopping; it said {seen}"
        seen.add(w.progress_msg.text())
        QApplication.processEvents()
        time.sleep(0.01)
    # Not overwritten by "Waiting 4 s…" from the requests winding down; then the result. Today is downloaded
    # first, so the result can name posts it saved before Stop ("… Stopped early.").
    result = w.progress_msg.text()
    assert "Stopping…" in seen and seen <= {"Stopping…", result}, seen
    assert result.startswith("Stopped before") or result.endswith("Stopped early."), result
    assert not env.warnings


def test_archive_slowdowns_are_waited_out(env):
    w = env.window()
    env.archive.fail(SEARCH, (422, {"error": "Timeout. Maybe slow down a bit"}),
                     (429, {"error": "Too many requests"}), (500, {}))
    fetch(w, comments=False)
    assert not env.warnings
    assert len(env.store().posts("python")) == in_range(env, w)


def test_an_archive_that_keeps_failing_shows_one_warning_and_unlocks_the_form(env):
    w = env.window()
    env.archive.fail(SEARCH, *[(400, {"error": "bad request"})] * 3)
    fetch(w, comments=False)
    assert len(env.warnings) == 1 and w.fetch_btn.isEnabled()


def test_an_empty_range_says_so(env):
    w = env.window()
    fetch(w, sub="nothinghere", comments=False)
    assert "No new posts" in w.progress_msg.text()
    assert env.store().posts("nothinghere") == []


def test_fetching_another_subreddit_opens_it_and_keeps_the_first(env):
    w = env.window()
    fetch(w, comments=False)
    assert w.current.lower() == "python"
    fetch(w, sub="rust", comments=False)
    names = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() for i in range(w.datasets.count())]
    assert set(names) == {"python", "rust"}
    assert w.current.lower() == "rust" and len(w.posts) == len(env.store().posts("rust"))


def test_enter_in_the_sidebar_fetches_once(env):
    w = env.window()
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(False)
    w.fetch_form.subreddit.setFocus()
    QTest.keyClick(w.fetch_form.subreddit, Qt.Key.Key_Return)
    QTest.keyClick(w.fetch_form.subreddit, Qt.Key.Key_Return)  # a second Enter while busy is ignored
    wait_idle(w)
    pump(300)
    assert not w.fetch_job.busy and env.archive.paths().count(AGG) == 0


@pytest.mark.parametrize("saved_before", [False, True])
def test_fetching_another_subreddit_shows_it_as_it_downloads(env, saved_before):
    # The window used to stay on whatever was open for the whole fetch, so nothing seemed to happen.
    w = env.window()
    if saved_before:
        fetch(w, comments=False)
    fetch(w, sub="rust", comments=False)
    assert w.current.lower() == "rust"
    env.archive.delay = 0.5  # a page at a time, so Stop lands partway
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    s = env.store()
    end = time.monotonic() + 20
    while len(s.posts("python")) < 4 + 2 * saved_before and time.monotonic() < end:
        pump(20)
    pump(300)  # a few fade-in ticks, well under the 1 s refresh
    assert w.fetch_job.busy, "the fetch should still be running"
    assert w.current.lower() == "python"
    assert w.posts_tab.model.rowCount() > 2 * saved_before
    # Stopped, as the delay above is for: the rest of the month at half a second a request took 24 s of
    # wait_idle's 30 on Linux, and longer on a Windows runner.
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    wait_idle(w)


def test_closing_mid_fetch_leaves_the_fetch_alone(env, monkeypatch):
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *e: errors.append(e))
    w = env.window()
    # A request Stop waits for is still out when the window closes: in top mode, the download of a ranked
    # day's picks. (A plain search no longer holds Stop up; that case is the test below.)
    env.archive.delays[IDS] = 4
    # The sidebar no longer ranks posts; the client still can, so the ranked fetch is started directly.
    w.start_request(mw.collect.FetchRequest("python", top=True), "Ranking posts…")
    # Close once that download is out: a fixed wait could close before it on a busy runner, and then Stop
    # had nothing left to wait for.
    end = time.monotonic() + 20
    while IDS not in env.archive.paths():
        assert time.monotonic() < end, "the ranked picks were never asked for"
        pump(20)
    w.close()
    assert w.running_threads(), "app.main ends the process instead of destroying this thread"
    end = time.monotonic() + 20
    while w.fetch_job.running() and time.monotonic() < end:
        pump(50)
    pump(200)
    assert errors == []  # the fetch ending didn't reach for the closed store


def test_a_save_reported_just_before_closing_isnt_handled_after_it(env, monkeypatch):
    # The fetch thread said "saved" a moment before the window closed. Its signal was still queued when
    # closeEvent disconnected the signals and closed the store, and was delivered anyway: the window read
    # the closed store ("Cannot operate on a closed database"; seen once on Windows CI).
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *e: errors.append(e))
    w = env.window()
    env.archive.delay = 4  # the fetch is still running when the window closes
    w.fetch_form.subreddit.setText("python")
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(300)
    worker_ = w.fetch_job.worker
    emitter = threading.Thread(target=worker_.saved.emit)  # from another thread, so it's queued
    emitter.start()
    emitter.join()
    w.close()
    pump(200)
    assert errors == []


def test_closing_while_a_search_is_out_ends_the_fetch_at_once(env, monkeypatch):
    # The setup the test above had before Stop stopped waiting for searches in flight.
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *e: errors.append(e))
    w = env.window()
    env.archive.delay = 4  # a request still out when the window closes
    w.fetch_form.subreddit.setText("python")
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(500)
    start = time.monotonic()
    w.close()
    assert time.monotonic() - start < 2 and not w.running_threads()
    pump(5000)  # the search's answer arrives after the window has gone
    assert errors == []


QUIT_MID_FETCH = """
import os, sys
from PySide6.QtCore import QSettings, QTimer
from subsentiment import arctic_shift, collect
from subsentiment.gui import app, main_window, worker
arctic_shift.BASE_URL = sys.argv[1]
worker.LIMITER.base = worker.LIMITER.interval = arctic_shift.BASE_INTERVAL / 10  # as in the other tests
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, sys.argv[2])
QSettings(app.STORAGE_ORG, app.STORAGE_APP).setValue("updates/auto", False)
app.default_db_path = lambda: os.path.join(sys.argv[2], "quit.db")
def window(path):
    w = main_window.MainWindow(path)
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    def close():
        print("saved", len(w.store.posts("python")) + len(w.store.comments("python")), flush=True)
        w.close()  # while a slow request for comments is still out
        print("running", bool(w.running_threads()), flush=True)
    def close_once_posts_are_saved():
        if sys.argv[3] == "idle":
            close()
        elif sys.argv[3] == "top":
            QTimer.singleShot(1500, close)  # the day's candidates are ranked; the download of the picks is out
        elif w.store.posts("python"):  # not a count: in the hour after 01:00 UTC today has only a post or two
            QTimer.singleShot(1500, close)  # the rest of the posts, then the comments start
        else:
            QTimer.singleShot(50, close_once_posts_are_saved)
    if sys.argv[3] == "top":  # the sidebar no longer ranks posts; the client still can
        QTimer.singleShot(300, lambda: w.start_request(
            collect.FetchRequest("python", limit=10, top=True), "Ranking posts…"))
    elif sys.argv[3] != "idle":
        QTimer.singleShot(300, w.fetch_btn.click)
    QTimer.singleShot(400, close_once_posts_are_saved)
    return w
app.MainWindow = window
sys.exit(app.main([]))
"""


# fetch: a comment request is still out at quit. Stop doesn't wait for comment trees, so the fetch ends
#   when the window closes, and the request left out must not hold up the process.
# top: a ranked fetch (top=True), quitting while the download of a day's ranked picks is out. Stop lets that request
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
    fetch(w, sub="rust", comments=False)
    fetch(w, sub="python", comments=False)  # python is open, as when fetching more of it
    env.archive.delay = 0.5  # a page at a time, so Stop lands partway
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
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
    assert w.fetch_job.busy, "the fetch should still be running"
    assert w.current.lower() == "rust" and shown == {"rust"}
    wait_idle(w)


def test_rows_keep_fading_in_while_comments_are_saved_quickly(env):
    # Each save used to restart the fade-in timer, so while comments were saved faster than it ticks,
    # no queued post appeared until the fetch ended.
    w = env.window()
    fetch(w, comments=False, rng="day")  # a dozen or so rows, as with the old Max posts of 12
    n = len(w.posts)
    assert 0 < n <= 18
    w.posts_tab.load([])
    w.request, w.fetch_job.qthread = mw.collect.FetchRequest("python"), object()  # as if python were being fetched
    w._read_to = {"posts": 0.0, "comments": 0.0}
    end = time.monotonic() + 1.5
    while w.posts_tab.model.rowCount() < n and time.monotonic() < end:
        w._on_saved()  # a comment saved every 10 ms
        pump(10)
    w._live.stop()
    w.fetch_job.qthread = None
    assert w.posts_tab.model.rowCount() == n


def test_a_big_download_reads_each_new_row_back_about_once(env, monkeypatch):
    # Every saved row used to re-read all of the fetch's rows so far, which froze the window on a day's
    # Download all (thousands of posts and comments).
    # Saved on a clock that moves in 15.6 ms steps, as Windows' does: a thread's comments saved together
    # shared one time, and each read got the whole batch back again (failed on Windows CI).
    monkeypatch.setattr(store_mod, "time", types.SimpleNamespace(time=lambda: int(time.time() * 64) / 64))
    w = env.window()
    fetch(w, comments=False)
    read = []
    for name in ("posts_since", "comments_since"):
        original = getattr(w.store, name)
        monkeypatch.setattr(w.store, name, lambda *a, f=original: read.append(len(rows := f(*a))) or rows)
    fetch(w, comments=True, rng="month")
    s = env.store()
    saved = len(s.posts("python")) + len(s.comments("python"))
    # How many of today's and yesterday's rows exist changes through the day, so the size check counts only
    # older days' rows: all of their posts, and 50 or more rows with comments (failed from 01:00 UTC).
    cut = NOW // DAY * DAY - DAY
    old = [r for r in s.posts("python") + s.comments("python") if r["created_utc"] < cut]
    assert len([r for r in s.posts("python") if r["created_utc"] < cut]) == len(
        [p for p in env.archive.posts if p["subreddit"] == "Python" and p["created_utc"] < cut
         and not p.get("removed_by_category")])
    assert len(old) >= 50 and sum(read) < 4 * saved  # linear; it was about saved² / 2


def test_a_busy_subreddit_that_times_out_still_fetches_with_narrower_queries(env, monkeypatch):
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.3)  # timeout back-offs, shortened further
    w = env.window()
    fetch(w, sub="askreddit", comments=False, rng="day")
    timeouts = [r for r in env.archive.rejected if r[2] == 422]
    assert timeouts and not env.warnings  # it hit the archive's timeouts and worked around them
    assert len(env.store().posts("askreddit")) >= 20


def test_a_keyword_search_the_archive_cant_run_says_so_once(env, monkeypatch):
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.3)  # timeout back-offs, shortened further
    w = env.window()
    w.fetch_form.query.setText("black holes")
    fetch(w, sub="askreddit", comments=False, rng="day")
    assert len(env.warnings) == 1 and "timed out" in env.warnings[0].lower()
    assert w.fetch_btn.isEnabled()


def test_a_fetch_at_the_apps_real_pace_never_trips_the_rate_limit(env, monkeypatch):
    # The real limiter (a request every 0.4 s) against a server allowing 3 a second.
    monkeypatch.setattr(worker.LIMITER, "base", arctic_shift.BASE_INTERVAL)
    monkeypatch.setattr(worker.LIMITER, "interval", arctic_shift.BASE_INTERVAL)
    env.archive.rate = (3, 1.0)
    w = env.window()
    fetch(w, comments=True, rng="day")
    assert [r for r in env.archive.rejected if r[2] == 429] == []
    assert len(env.archive.paths()) >= 4


def test_when_the_server_says_no_requests_are_left_the_app_waits_for_the_reset(env):
    w = env.window()
    env.archive.rate = (2, 1.0)  # far below the test pace
    fetch(w, comments=True, rng="day")
    assert [r for r in env.archive.rejected if r[2] == 429] == []  # it waited instead of hitting the limit
    assert not env.warnings and len(env.store().posts("python")) == in_range(env, w)


def open_coverage(w):
    w.coverage_act.trigger()
    end = time.monotonic() + 30
    while (w.coverage.count_job.busy or not w.coverage.days) and time.monotonic() < end:
        pump(20)
    pump(50)
    return w.coverage


def test_coverage_counts_a_busy_subreddit_without_downloading_anything(env, monkeypatch):
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.3)  # timeout back-offs, shortened further
    w = env.window()
    fetch(w, sub="askreddit", comments=False, rng="day")
    env.archive.clear()
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    cov = open_coverage(w)
    paths = set(env.archive.paths())
    assert paths == {AGG}  # only counts; the month-long count timed out and was split, never refused
    assert [r for r in env.archive.rejected if r[2] == 422]
    totals = cov.totals["askreddit"]
    assert sum(totals.values()) == len([p for p in env.archive.posts if p["subreddit"] == "AskReddit"
                                        and p["created_utc"] >= w.fetch_form.bounds()[0]])
    for row in range(min(5, cov.day_table.rowCount())):  # browsing days and posts
        cov.day_table.selectRow(row)
        pump(30)
    QTest.keyClick(cov.day_table, Qt.Key.Key_Return)
    QTest.keyClick(cov.post_table, Qt.Key.Key_Return)
    pump(500)
    assert set(env.archive.paths()) == {AGG} and not w.fetch_job.busy


def test_coverage_counts_posts_removed_from_reddit_as_checked(env):
    # Every fifth post in the fake archive was removed. A day's count includes them, so once Download all
    # has been through a day, it reads complete, and before that its share counts the removed ones seen.
    w = env.window()
    fetch(w, comments=False)
    cov = open_coverage(w)
    # The sidebar's fetch read every whole past day of the week to its end, so those days already read
    # complete. Forget that, as for days saved before fetches did, to see a day's share before Download all.
    whole = [r for r, d in enumerate(cov.days) if d >= w.fetch_form.bounds()[0] and d < coverage.today()]
    assert whole and all(cov.day_table.cellWidget(r, 1).side.text() == "✓ Complete" for r in whole)
    w.store.db.execute("DELETE FROM complete")
    w.store.commit()
    cov.refresh_saved(rebuild=True)
    pump(50)
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
    base = make_posts(sub, NOW, days=2, per_day=(1,), seed=4)[0]  # days=2: in the hour after midnight UTC, today has none
    env.archive.posts += [dict(base, id=f"bd{i:05d}", title=f"question {i}", created_utc=day + 30 + i * 50,
                               num_comments=3, permalink=f"/r/{sub}/comments/bd{i:05d}/x/") for i in range(n)]
    return day


def wait_idle_stalls(w, timeout: float = 60) -> float:
    """wait_idle, returning the longest the window went without handling events (a freeze)."""
    end, worst = time.monotonic() + timeout, 0.0
    while w.fetch_job.busy:
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
    # Saving the 1,500 posts first takes about 3 s here and went past 30 s once on a loaded Windows runner;
    # this test times the Download all below, not this.
    fetch(w, sub="bigday", comments=False, timeout=120)
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


def test_download_all_comments_on_new_posts_says_what_it_got_and_doesnt_offer_them_again(env):
    # Posts under 36 hours old have no comment count yet. Download all comments fetched their threads,
    # but the rows still read "not counted yet" and the button still offered the same posts, every time.
    w = env.window()
    fetch(w, comments=False)
    cov = open_coverage(w)
    row = next(r for r, d in enumerate(cov.days)
               if any(p["num_comments"] is None for p in w.store.comment_coverage("python", d, d + DAY)))
    cov.day_table.selectRow(row)
    pump(50)
    n = cov.post_table.rowCount()
    assert cov.all_comments.text() == f"Download all comments ({n} posts)"
    QTest.mouseClick(cov.all_comments, Qt.MouseButton.LeftButton)
    wait_idle(w)
    pump(100)
    assert cov.all_comments.text() == "Download all comments" and not cov.all_comments.isEnabled()
    for i in range(n):
        cov.post_table.scrollTo(cov.post_table.model().index(i, 0))  # rows get their widgets once on screen
        pump(10)
        cell, button = cov.post_table.cellWidget(i, 1), cov.post_table.cellWidget(i, 2).findChild(type(w.fetch_btn))
        assert cell.side.text() == "all so far" and "36 hours" in cell.toolTip()
        assert button.text() == "Check again" and button.isEnabled()
    env.archive.clear()
    QTest.mouseClick(cov.post_table.cellWidget(0, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    wait_idle(w)
    assert [p for p, _ in env.archive.requests if p == TREE] == [TREE]  # Check again still fetches it


def test_a_days_download_all_that_hits_a_window_the_archive_cant_answer_saves_the_rest(env, monkeypatch):
    # Live on Sat 17 Oct, one window kept timing out however narrow, and Download all ended in a "Fetch
    # failed" error box with 69% of the day saved. Now the rest of the day is read, there's no box, the
    # day isn't called complete, and the line under the bar says what happened.
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 0.2)  # timeout back-offs, shortened further
    w = env.window()
    fetch(w, comments=False, rng="day")  # the day below isn't saved whole, so it can't be complete yet
    cov = open_coverage(w)
    row, day = next((r, d) for r, d in enumerate(cov.days) if cov.totals["python"].get(d) and d < coverage.today())
    in_day = sorted(p["created_utc"] for p in env.archive.posts
                    if p["subreddit"] == "Python" and day <= p["created_utc"] < day + DAY)
    env.archive.stuck = [in_day[len(in_day) // 2] - 1]
    QTest.mouseClick(cov.day_table.cellWidget(row, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    wait_idle(w)
    pump(100)
    assert env.warnings == []
    assert "timed out" in w.progress_msg.text() and "Fetch failed" not in w.progress_msg.text()
    assert cov.day_table.cellWidget(row, 1).side.text() != "✓ Complete"
    saved = {r["created_utc"] for r in w.store.posts("python")}
    assert in_day[0] in saved and in_day[-1] in saved  # both ends of the day were still read


def test_coverage_downloads_do_only_what_they_say(env):
    w = env.window()
    fetch(w, comments=False)
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
    fetch(w, comments=False)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    cov = open_coverage(w)
    last = cov.day_table.rowCount() - 1
    cov.day_table.selectRow(last)
    cov.day_table.scrollToBottom()
    pump(50)
    scrolled = cov.day_table.verticalScrollBar().value()
    assert scrolled > 0
    env.archive.delay = 0.05
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(2500)  # the page refreshes about once a second while rows arrive
    assert cov.day_table.verticalScrollBar().value() == scrolled
    assert cov._selected_day() == cov.days[last]
    wait_idle(w)


def test_coverage_counts_a_subreddit_picked_while_another_is_being_counted(env):
    # The first count's end used to be taken for days that couldn't be counted: the new subreddit was
    # never counted and every day read "total unknown", with no message, until Recount.
    w = env.window()
    fetch(w, comments=False)
    fetch(w, sub="rust", comments=False)
    w.datasets.setCurrentRow(next(i for i in range(w.datasets.count())
                                  if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "python"))
    env.archive.delays[AGG] = 1.0
    w.coverage_act.trigger()
    pump(200)
    assert w.coverage.count_job.busy  # python's count is still out
    w.datasets.setCurrentRow(next(i for i in range(w.datasets.count())
                                  if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "rust"))
    end = time.monotonic() + 15
    while ("rust" not in w.coverage.totals or w.coverage.count_job.busy) and time.monotonic() < end:
        pump(20)
    cov = w.coverage
    assert cov.subreddit.lower() == "rust" and cov.count_error == ""
    assert all(d in cov.totals["rust"] for d in cov.days)


def test_coverage_days_wait_for_their_count_before_they_can_be_downloaded(env):
    # A day still being counted read "0 saved" with Download all live, as if there were nothing to fetch or
    # it could be fetched blind. Until its count is in it shows a throbber and can't be downloaded, alone or
    # ticked; today (never counted) and complete days (no count needed) stay usable throughout.
    w = env.window()
    fetch(w, comments=False)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    complete = env.store().complete_keys("posts", "python")
    env.archive.clear()
    env.archive.delays[AGG] = 1.5
    w.coverage_act.trigger()
    pump(200)
    cov = w.coverage
    assert cov.count_job.busy

    def rows():
        for row, day in enumerate(cov.days):
            yield day, cov.day_table.cellWidget(row, 1), cov.day_table.cellWidget(row, 2).findChild(QPushButton)

    waiting = []
    for day, cell, button in rows():
        if day >= coverage.today() or str(day) in complete:
            assert button.isEnabled() and not cell.bar.waiting, day
        else:
            assert cell.bar.waiting and cell.side.text() == "counting…" and not button.isEnabled(), day
            waiting.append(day)
    done = [d for d in cov.days if str(d) in complete]
    assert waiting and done
    cov._tick(done[0], True)
    assert cov.download_ticked.isEnabled()  # a complete day can be checked again at once
    cov._tick(waiting[0], True)
    assert not cov.download_ticked.isEnabled()
    end = time.monotonic() + 15
    while cov.count_job.busy and time.monotonic() < end:
        pump(20)
    pump(50)
    totals = cov.totals["python"]
    assert not any(cell.bar.waiting for _, cell, _ in rows())
    # Every day with posts to look for can be downloaded now; only past days the archive has none for can't.
    assert all(button.isEnabled() == (totals.get(day) != 0 or str(day) in complete or day == cov.days[0])
               for day, _, button in rows())
    assert cov.download_ticked.isEnabled()
    assert set(env.archive.paths()) == {AGG}  # waiting downloaded nothing


def test_coverage_follows_a_change_of_date_range_in_the_sidebar(env):
    w = env.window()
    fetch(w, comments=False)
    cov = open_coverage(w)
    assert len(cov.days) == 8
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    pump(500)
    assert len(cov.days) == 31 and cov.day_table.rowCount() == 31
    w.fetch_form.date_from.setDate(w.fetch_form.date_to.date().addDays(-2))  # typing a date: a custom range
    pump(500)
    assert len(cov.days) == 3


def test_closing_the_app_while_a_coverage_count_is_out_doesnt_freeze_it(env):
    w = env.window()
    fetch(w, comments=False)
    env.archive.delays[AGG] = 8
    w.coverage_act.trigger()
    pump(200)
    assert w.coverage.count_job.busy
    start = time.monotonic()
    w.close()
    # It used to wait for the request. Timed against the wall clock this read the store's own close too, which
    # took 2.5 s on a Windows runner; the request still out afterwards shows nothing waited for it.
    assert w.coverage.count_job.busy and time.monotonic() - start < 8
    end = time.monotonic() + 20
    while w.coverage.count_job.busy and time.monotonic() < end:
        pump(20)
    assert not w.coverage.count_job.busy  # the count still ends and is tidied up


def test_a_fetch_ending_leaves_the_user_on_the_subreddit_they_switched_to(env):
    w = env.window()
    fetch(w, sub="rust", comments=False)
    fetch(w, comments=False)
    env.archive.delay = 0.15
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    pump(600)
    w.datasets.setCurrentRow(next(i for i in range(w.datasets.count())
                                  if w.datasets.item(i).data(Qt.ItemDataRole.UserRole).lower() == "rust"))
    pump(50)
    assert w.fetch_job.busy and w.current.lower() == "rust"
    wait_idle(w, 60)
    assert w.current.lower() == "rust" and w.datasets.currentItem().text().lower() == "r/rust"
    assert "Added" in w.progress_msg.text()


def test_stop_on_the_coverage_page_is_disabled_once_pressed(env):
    w = env.window()
    fetch(w, comments=False)
    cov = open_coverage(w)
    # The day's Download all is four requests sent at once, so at 0.15 s each it was over in about 0.34 s
    # and Stop was sometimes already off when the test looked at 0.3 s.
    env.archive.delay = 1.0
    QTest.mouseClick(cov.day_table.cellWidget(1, 2).findChild(type(w.fetch_btn)), Qt.MouseButton.LeftButton)
    pump(300)
    assert cov.isVisible() and w.stop_btn.isVisible() and w.stop_btn.isEnabled()  # the strip under the header
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    assert not w.stop_btn.isEnabled()
    w.coverage_act.trigger()  # opening the page again while it stops
    assert not w.stop_btn.isEnabled()
    wait_idle(w)


def test_coverage_left_and_opened_again_mid_count_counts_without_an_error(env):
    w = env.window()
    fetch(w, comments=False)
    env.archive.delays[AGG] = 1.5
    w.coverage_act.trigger()
    pump(200)
    w.show_page("overview")  # the count goes on while another page shows
    w.coverage_act.trigger()
    end = time.monotonic() + 15
    while ("python" not in w.coverage.totals or w.coverage.count_job.busy) and time.monotonic() < end:
        pump(20)
    pump(100)
    cov = w.coverage
    assert cov.count_error == "" and all(d in cov.totals["python"] for d in cov.days)


def test_choosing_a_span_on_the_timeline_never_downloads_and_a_fetch_only_adds_rows_from_it(env):
    """Ame: the Overview's sentiment follows the stretch of time picked on the chart, and the Days picker that
    used to choose it is gone. Dragging across the timeline counts only those days on every page."""
    w = env.window()
    fetch(w, comments=False)  # the past week, so the month's fetch below adds more
    saved = w.store.day_counts("python")
    first = min(saved)
    env.archive.clear()

    tl = w.overview.timeline
    plot, _ = tl._areas()
    lo, hi, _ = tl._screen
    x0, x1 = (round(tl._x(t, lo, hi, plot)) for t in (first + 0.2 * DAY, first + 1.8 * DAY))
    y = round(plot.center().y())
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(x0, y))
    QTest.mouseMove(tl, QPoint(x1, y))
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(x1, y))
    first_day, end = days.whole(w.span)
    chosen = {d for d in saved if first_day <= d < end}
    want = sum(saved[d][0] for d in chosen)
    assert first in chosen and len(chosen) < len(saved)
    assert w.overview.tiles["count"].value.text() == f"{want:,}"  # at once, before the tables follow
    for page in PAGES:  # Download coverage lists the Fetch range, not the chosen days, and asks for counts
        if page != "coverage":
            w.show_page(page)
            pump(30)
    pump(1200)
    assert env.archive.paths() == []  # choosing days only filters what's saved
    assert len(w.posts) == w.posts_tab.model.rowCount() == want
    assert sum(w.overview.distribution._counts.values()) == want

    env.archive.delay = 0.05
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    while w.fetch_job.busy:  # rows from other days are saved, and on the timeline, but not in the tables
        QApplication.processEvents()
        rows = [w.posts_tab.model.row_at(i) for i in range(w.posts_tab.model.rowCount())]
        assert all(first_day <= r["created_utc"] < end for r in rows)
        time.sleep(0.01)
    pump(400)
    env.archive.delay = 0
    every = env.store().posts("python")
    assert days.whole(w.span) == (first_day, end)  # earlier days were added, not later ones: the span stays
    in_span = [r for r in every if first_day <= r["created_utc"] < end]
    assert w.posts_tab.model.rowCount() == len(in_span) == len(w.posts)
    assert sum(n for _, _, n in tl.points) == len(every) > len(in_span)  # the timeline has every day
    assert w.overview.tiles["count"].value.text() == f"{len(in_span):,}"


def test_a_second_fetch_into_a_shown_subreddit_ends_without_reading_it_all_back(env, monkeypatch):
    w = env.window()
    fetch(w, rng="day")
    full_reads = []
    for name in ("posts", "comments"):
        real = getattr(Store, name)
        monkeypatch.setattr(Store, name, lambda self, *a, real=real, name=name: (
            full_reads.append(name) if self is w.store else None, real(self, *a))[1])
    fetch(w, refresh_scores=True)  # the week: more posts and comments, and saved ones re-checked
    assert full_reads == []  # it used to read every saved row again as the fetch ended
    store = env.store()
    for tab, saved in ((w.posts_tab, store.posts("python")), (w.comments_tab, store.comments("python"))):
        assert tab.model.rowCount() == len(saved) == len(tab.all_rows)
        shown = {tab.model.row_at(i)["id"]: tuple(tab.model.row_at(i)) for i in range(tab.model.rowCount())}
        assert shown == {r["id"]: tuple(r) for r in saved}  # new scores included
    assert w.overview.tiles["count"].value.text() == f"{len(store.posts('python')) + len(store.comments('python')):,}"


def test_the_word_cloud_keeps_its_words_in_place_while_a_fetch_adds_to_them(env):
    """Ame: the cloud "should update dynamically while you scrape instead of constantly resetting". Each refresh
    used to lay every word out again from the middle, so all of them jumped about; now a word stays where it is
    and only grows or shrinks, unless something bigger needs its place."""
    w = env.window()
    w.show_page("words")
    env.archive.delay = 0.03  # a few refreshes of the cloud while it runs
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    cloud, shown = w.words_page.body, []
    while w.fetch_job.busy:
        pump(120)
        shown.append({word: box.center() for box, word, *_ in cloud.placed()})
    # How far the words moved between two looks showing the same words with new counts: the median, for each
    # pair. (Which looks catch words arriving depends on timing: the first day's few words all grow several
    # times over as more come in, and new big ones take the middle.) That includes looks across the fetch's
    # strip appearing under the header, which makes the page a little shorter.
    moves = []
    for a, b in zip(shown, shown[1:]):
        if a and a != b and set(a) == set(b):
            d = sorted((a[k] - b[k]).manhattanLength() for k in set(a) & set(b))
            moves.append(d[len(d) // 2])
    assert len(moves) >= 3  # the cloud changed during the fetch
    assert max(moves) < 15, moves  # before #112 the median word moved 95-190 px each time


def test_every_view_keeps_up_with_the_rows_shown_while_a_fetch_waits(env):
    """Ame: "basically I just want everything to update live". The overview used to be redrawn only a second
    after a save, from the rows dripped into the tables by then, so while the fetch waited on a slow request
    the tables kept filling and the overview, day picker and coverage stood still behind them."""
    w = env.window()
    # The first day's posts come at once, more than the tables show in a second; then a long wait on that
    # day's comments before the next day's posts (#69), so `want` is what is saved once that wait starts.
    # Yesterday's: today's grow through the day from 01:00 UTC, a post every 10 minutes.
    env.archive.posts += make_posts("Burst", NOW, days=4, per_day=(0, 80), seed=4)
    env.archive.delays[TREE] = 20  # long enough that no comment lands before the checks below, even on a busy runner
    w.fetch_form.subreddit.setText("Burst")
    w.fetch_form.include_comments.setChecked(True)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    end = time.monotonic() + 20
    store, want = env.store(), 0
    while not want or w.posts_tab.model.rowCount() < want or w._drip.isActive():
        assert time.monotonic() < end and w.fetch_job.busy, w.posts_tab.model.rowCount()
        if "comments on" in w.progress_msg.text():  # the first day's posts are all saved by now
            want = want or len(store.posts("Burst"))
        if w.current and not w.coverage.isVisible():  # opened with the first rows, to watch the rest come in
            w.show_page("coverage")
        pump(5)
    pump(1200)  # past the overview's 250 ms and the saved-data list's 1 s; still waiting on the comments  # a little more than the overview's 250 ms; still waiting on the comments
    assert w.fetch_job.busy and not w.comments_tab.model.rowCount()
    rows = w.posts_tab.all_rows
    o = w.overview
    assert len(rows) == want
    assert o.tiles["count"].value.text() == f"{want:,}"
    assert sum(o.distribution._counts.values()) == want
    assert o.distribution._counts == {k: sum(r["label"] == k for r in rows) for k in ("negative", "neutral", "positive")}
    assert sum(n for _, _, n in o.timeline.points) == want
    assert f"· {want:,}\xa0posts ·" in w.subheading.full_text() and w.subheading.text().startswith("Last fetched")
    assert w._analysis["posts"].count == want
    words = {word: n for word, n, _ in w.word_tally.top(["posts", "comments"], 500)}
    for word in ("bug", "update", "thanks"):  # the cloud counts the posts using each word as they're shown
        assert words.get(word, 0) == sum(word in r["title"].split() for r in rows) > 0, word
    assert {word for _, word, *_ in o.words.placed()} <= set(words) and o.words.placed()
    pump(800)  # what reads the store again does so once a second
    assert w.fetch_job.busy
    assert w.storage_info.text().startswith(f"{want}\xa0posts")
    c = w.coverage
    saved = env.store().posts_per_day("Burst", c.days[-1], c.days[0] + DAY)
    for row, day in enumerate(c.days):
        shown = c.day_table.cellWidget(row, 1).main.text()  # "12 of 20", "3 saved so far" or "No posts"
        assert int(re.match(r"[\d,]*", shown).group().replace(",", "") or 0) == saved.get(day, 0), (day, shown)
    w._stop_fetch()
    wait_idle(w)


def test_data_saved_by_an_older_ikari_opens_shrunk_and_still_links_each_comment(env, monkeypatch):
    shutil.copy(Path(__file__).parent / "data" / "store-53829f7.db", env.db)
    # Its text is packed into compressed day packs. The file is a few pages, one per table, so its size
    # can't show that (the packs table adds a page); the bytes of text stored can.
    text = ("SELECT (SELECT SUM(LENGTH(CAST(title || selftext || author || IFNULL(permalink, '') || IFNULL(url, '')"
            " AS BLOB))) FROM posts) + (SELECT SUM(LENGTH(CAST(IFNULL(parent_id, '') || author || body AS BLOB)))"
            " FROM comments)")
    db = sqlite3.connect(env.db)
    before = db.execute(text).fetchone()[0]
    db.close()
    opened = []
    monkeypatch.setattr(tables.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    w = env.window()
    w.upgrading.join()  # packed on its own thread while the window is up
    db = sqlite3.connect(env.db)
    assert db.execute("SELECT COUNT(*) FROM posts WHERE title IS NOT NULL").fetchone()[0] == 0
    assert db.execute("SELECT SUM(LENGTH(data)) FROM packs").fetchone()[0] < before * 0.6
    db.close()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.show_page("comments")
    page = w.comments_tab
    assert page.model.rowCount() == 11
    for i in range(page.model.rowCount()):
        page.view.doubleClicked.emit(page.model.index(i, 0))
    old = sqlite3.connect(Path(__file__).parent / "data" / "store-53829f7.db")
    assert sorted(opened) == sorted(link for (link,) in old.execute("SELECT permalink FROM comments"))
    old.close()
    fetch(w, comments=True)  # new comments are saved into it the same way
    s = env.store()
    fetched = [r for r in s.comments("python") if r["post_id"].startswith("py")]
    assert fetched and all(r["permalink"] for r in fetched)
    stored = dict(s.db.execute("SELECT id, permalink FROM comments WHERE post_id LIKE 'py%'"))
    assert set(stored.values()) == {None}  # each is its post's permalink and id, so none is stored


def test_a_zoomed_timeline_keeps_up_with_a_fetch(env):
    """The timeline stays live while zoomed: "1w" picked mid-fetch still shows the latest week once every day
    is in, and the navigator under it every day."""
    w = env.window()
    env.archive.posts += make_posts("Zoom", NOW, days=20, per_day=(5,), seed=6)
    w.fetch_form.subreddit.setText("Zoom")
    w.fetch_form.include_comments.setChecked(False)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    tl = w.overview.timeline
    end = time.monotonic() + 20
    while not tl.presets["1w"].isEnabled():
        assert time.monotonic() < end and w.fetch_job.busy, len(tl.points)
        pump(5)
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    assert tl.preset == "1w"
    wait_idle(w)
    pump(400)  # the overview's last 250 ms refresh
    saved = {int(r["created_utc"] // DAY * DAY) for r in env.store().posts("Zoom")}
    assert [d for d, _, _ in tl.points] == sorted(saved)
    a, b = tl.domain()
    assert tl.view == (b - 7 * DAY, b) and tl.preset == "1w" and (a, b) == (min(saved), max(saved) + DAY)
    week = [r for r in env.store().posts("Zoom") if r["created_utc"] >= b - 7 * DAY]
    assert w.overview.tiles["count"].value.text() == f"{len(week):,}"  # the tiles count the week in view
    pump(400)  # the tables' settling delay
    assert len(w.posts) == w.posts_tab.model.rowCount() == len(week)


def test_a_fetchs_settled_days_are_packed_and_still_show_what_was_fetched(env, monkeypatch):
    opened = []
    monkeypatch.setattr(tables.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    w = env.window()
    fetch(w, comments=True)
    w.close()
    s = env.store()
    packed = dict(s.db.execute("SELECT packed, COUNT(*) FROM posts GROUP BY packed"))
    assert packed.get(1) and packed.get(0)  # days older than two are packed, the last two are left plain
    assert s.db.execute("SELECT COUNT(*) FROM comments WHERE packed = 1 AND body IS NULL").fetchone()[0]
    archive = {p["id"]: p for p in env.archive.posts}

    w = env.window()  # a restart reads the packed days back
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.show_page("posts")
    saved = lambda p: (p["title"], p["selftext"].replace("[removed]", ""), p["author"])  # noqa: E731
    assert w.posts and all(saved(r) == saved(archive[r["id"]]) for r in w.posts)
    w.show_page("comments")
    page = w.comments_tab
    assert page.model.rowCount() == len(s.comments("python")) > 0
    assert {r["body"] for r in s.comments("python")} == {"nice", "awful", "ok"}
    for i in range(page.model.rowCount()):
        page.view.doubleClicked.emit(page.model.index(i, 0))
    assert sorted(opened) == sorted("https://www.reddit.com" + archive[r["post_id"]]["permalink"] + r["id"] + "/"
                                    for r in s.comments("python"))


def test_a_stopped_download_all_goes_on_from_where_it_got_to(env, monkeypatch):
    # Run again after Stop (or after closing the app), Download all paged through every post it already had
    # before reaching new ones, so a resumed day took as long as a fresh one and, on a busy subreddit, sat on
    # "Arctic Shift is slow" for minutes without saving anything.
    day = busy_day(env, "bigday", 1500)
    env.archive.posts += [dict(env.archive.posts[-1], id="other", created_utc=NOW - 3600)]
    w = env.window()
    fetch(w, sub="bigday", comments=False, rng="day")  # a post from today, so r/bigday is saved
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    cov = open_coverage(w)
    row = cov.days.index(day)
    button = lambda: cov.day_table.cellWidget(row, 2).findChild(type(w.fetch_btn))  # noqa: E731
    env.archive.delay = 1.0  # a page a second for each part of the day, so Stop lands partway
    env.archive.clear()
    QTest.mouseClick(button(), Qt.MouseButton.LeftButton)
    end = time.monotonic() + 30
    in_day = lambda: {r["id"] for r in env.store().posts("bigday") if day <= r["created_utc"] < day + DAY}  # noqa: E731
    while len(env.archive.paths()) < 6 and time.monotonic() < end:  # the first pages are in, the next asked for
        pump(20)
    QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
    wait_idle(w)
    env.archive.delay = 0
    saved = in_day()
    assert 300 <= len(saved) < 1500
    env.archive.clear()
    QTest.mouseClick(button(), Qt.MouseButton.LeftButton)
    wait_idle(w, 60)
    resumed = [q for p, q in env.archive.requests if p == SEARCH]
    assert len(in_day()) == 1500 and str(day) in env.store().complete_keys("posts", "bigday")
    # Each part of the day is read on from where the first run got to: the saved posts aren't asked for again.
    again = 0
    for q in resumed:
        rows = sorted(p["created_utc"] for p in env.archive.posts if p["subreddit"] == "bigday"
                      and float(q["after"]) < p["created_utc"] < float(q["before"]))[:int(q["limit"])]
        again += sum(1 for p in env.archive.posts if p["subreddit"] == "bigday" and p["created_utc"] in rows
                     and p["id"] in saved)
    assert again <= 8  # a post or two where each part left off


def test_a_count_that_keeps_timing_out_doesnt_hold_up_a_download(env, monkeypatch):
    # The Coverage page counts its days while a Download all runs. Each time the count timed out, every request
    # was held back 2 s, then 4 s, so the download beside it said "Arctic Shift is slow" over and over.
    monkeypatch.setattr(arctic_shift, "MAX_WAIT", 120.0)  # the app's real back-offs
    w = env.window()
    fetch(w, comments=False, rng="day")
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    env.archive.clear()
    env.archive.fail(AGG, *[SLOW_DOWN] * 100)
    w.coverage_act.trigger()
    end = time.monotonic() + 10
    while not env.archive.paths() and time.monotonic() < end:
        pump(10)
    pump(100)  # its answer is in: the count waits out its first timeout
    assert w.coverage.count_job.busy and env.archive.paths() == [AGG]
    cov = w.coverage
    row = next(r for r, d in enumerate(cov.days) if d < coverage.today() - DAY
               and any(d <= p["created_utc"] < d + DAY for p in env.archive.posts if p["subreddit"] == "Python"))
    # An uncounted day's Download all now waits for its count, so the same download is started as its button
    # would once enabled: what's checked here is that the count's back-offs don't hold up a download.
    assert not cov.day_table.cellWidget(row, 2).findChild(type(w.fetch_btn)).isEnabled()
    cov._download_day(cov.days[row])
    wait_idle(w)
    paths = env.archive.paths()
    assert SEARCH in paths and paths.count(AGG) == 1  # done within the count's first 2 s pause, not after it
    assert env.store().posts_per_day("python", cov.days[row], cov.days[row] + DAY)
    assert w.coverage.count_job.busy  # still waiting out its own timeouts
    w.close()  # which stops the count: its pause notices within a fraction of a second
    end = time.monotonic() + 5
    while w.coverage.count_job.busy and time.monotonic() < end:
        pump(20)
    assert not w.coverage.count_job.busy


def test_starting_a_new_subreddit_switches_to_it_at_once(env):
    w = env.window()
    fetch(w, comments=False)
    assert w.current.lower() == "python"
    env.archive.delay = 0.5  # nothing of r/rust is saved for a good while
    w.fetch_form.subreddit.setText("rust")
    w.fetch_form.include_comments.setChecked(False)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    assert w.fetch_job.busy and w.current.lower() == "rust" and w.posts_tab.model.rowCount() == 0
    assert w.datasets.currentItem() is None and w.coverage.subreddit.lower() == "rust"
    env.archive.delay = 0
    wait_idle(w)
    assert w.current.lower() == "rust" and w.posts_tab.model.rowCount() > 0
    assert w.datasets.currentItem().data(Qt.ItemDataRole.UserRole).lower() == "rust"


def test_a_new_subreddit_that_saves_nothing_goes_back_to_a_saved_one(env):
    w = env.window()
    fetch(w, comments=False)
    fetch(w, sub="nosuchsub", comments=False)
    assert w.current.lower() == "python" and w.datasets.currentItem() is not None


def test_starting_a_saved_subreddit_after_another_switches_to_it_at_once(env):
    w = env.window()
    fetch(w, comments=False)
    fetch(w, sub="rust", comments=False)
    assert w.current.lower() == "rust"
    env.archive.delay = 0.5
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.include_comments.setChecked(False)
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    assert w.fetch_job.busy and w.current.lower() == "python" and w.posts_tab.model.rowCount() > 0
    env.archive.delay = 0
    wait_idle(w)


def test_coverage_downloads_a_days_comments_many_threads_at_once(env, monkeypatch):
    # Live on 21 Oct, Download all comments went through a day's posts about two a second: four threads in
    # flight, each started 0.4 s after the last, while the archive took 1-2.5 s to answer each.
    monkeypatch.setattr(worker.LIMITER, "base", arctic_shift.BASE_INTERVAL)  # the app's real pace
    monkeypatch.setattr(worker.LIMITER, "interval", arctic_shift.BASE_INTERVAL)
    day = busy_day(env, "bigday", 40)
    env.archive.posts += [dict(env.archive.posts[-1], id="other", created_utc=NOW - 3600)]
    w = env.window()
    fetch(w, sub="bigday", comments=False, rng="week")
    cov = open_coverage(w)
    cov.day_table.selectRow(cov.days.index(day))
    pump(50)
    assert cov.all_comments.text() == "Download all comments (40 posts)"
    env.archive.clear()
    env.archive.peak = 0
    env.archive.delays[TREE] = 1.6  # as long as the archive takes
    started = time.monotonic()
    QTest.mouseClick(cov.all_comments, Qt.MouseButton.LeftButton)
    wait_idle(w, 60)
    took = time.monotonic() - started
    assert [p for p, _ in env.archive.requests].count(TREE) == 40
    assert env.archive.peak == arctic_shift.TREE_WORKERS
    starts = 40 * arctic_shift.BASE_INTERVAL * arctic_shift.TREE_SHARE
    assert took < 40 * arctic_shift.BASE_INTERVAL  # on main, 4 at a time took 40 / 4 * 1.6 s = 16 s
    assert took >= starts  # still spaced out: at most one start every 0.2 s
