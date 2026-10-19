"""The fetch form and saved-data list survive a restart; the delete actions clear the store."""
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.reddit import Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402


@pytest.fixture
def make_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    # INI files under tmp_path rather than the real settings store (the registry on Windows).
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    db = tmp_path / "w.db"
    s = Store(db)
    for i, sub in enumerate(("Python", "rust")):
        s.save_post(Post(f"p{i}", sub, "nice", "", "a", 1, 0.9, 0, 1_700_000_000.0, "https://r", ""), 0.5, "positive")
    s.commit()
    s.close()
    windows = []

    def _make():
        w = mw.MainWindow(str(db))
        windows.append(w)
        return w
    yield _make
    for w in windows:
        w.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_fetch_form_and_viewed_dataset_are_restored(make_window):
    w = make_window()
    w.subreddit.setText("r/AskScience")
    w.query.setText("black holes")
    w.date_from.setDate(QDate(2026, 3, 1))  # editing a date switches to a custom range
    w.date_to.setDate(QDate(2026, 3, 31))
    w.pick.setCurrentIndex(w.pick.findData("top"))
    w.limit.setValue(250)
    w.include_comments.setChecked(False)
    w.comments_per_post.setValue(20)
    assert not w.refresh_scores.isChecked()  # off by default
    w.refresh_scores.setChecked(True)
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.tabbar.setCurrentIndex(1)
    w.close()

    again = make_window()
    assert again.subreddit.text() == "r/AskScience" and again.query.text() == "black holes"
    assert again.range.currentData() == "custom"
    assert (again.date_from.date(), again.date_to.date()) == (QDate(2026, 3, 1), QDate(2026, 3, 31))
    assert (again.limit.value(), again.include_comments.isChecked(), again.comments_per_post.value()) == (250, False, 20)
    assert again.pick.currentData() == "top" and "36 hours" in again.pick.toolTip()
    assert not again.comments_per_post.isEnabled()
    assert again.refresh_scores.isChecked()
    assert again.current == "Python" and again.tabbar.currentIndex() == 1


def test_comments_are_fetched_by_default_and_presets_fill_the_dates(make_window):
    w = make_window()
    assert w.include_comments.isChecked() and w.range.currentData() == "week"
    today = QDate.currentDate()
    assert (w.date_from.date(), w.date_to.date()) == (today.addDays(-7), today)
    w.range.setCurrentIndex(w.range.findData("month"))
    assert w.date_from.date() == today.addDays(-30) and w.range.currentData() == "month"


def test_range_bounds():
    now = datetime(2026, 10, 19, 12, 0)
    assert mw.range_bounds("day", QDate(), QDate(), now) == ((now - timedelta(days=1)).timestamp(), None)
    after, before = mw.range_bounds("custom", QDate(2026, 3, 31), QDate(2026, 3, 1), now)  # reversed is fine
    assert after == datetime(2026, 3, 1).timestamp() and before == datetime(2026, 4, 1).timestamp()


def test_fetch_builds_a_date_range_request(make_window, monkeypatch):
    sent = []
    monkeypatch.setattr(mw, "FetchWorker", lambda req, db: sent.append(req) or (_ for _ in ()).throw(RuntimeError))
    w = make_window()
    w.subreddit.setText("python")
    w.date_from.setDate(QDate(2026, 3, 1))
    w.date_to.setDate(QDate(2026, 3, 2))
    with pytest.raises(RuntimeError):
        w._start_fetch()
    (req,) = sent
    assert (req.after, req.before) == (datetime(2026, 3, 1).timestamp(), datetime(2026, 3, 3).timestamp())
    assert req.include_comments


def test_invalid_subreddit_is_explained_without_starting_a_fetch(make_window):
    w = make_window()
    w.subreddit.setText("not a sub")
    w._start_fetch()
    assert w.thread is None and "isn't a valid subreddit name" in w.progress_msg.text()


def test_delete_one_then_all(make_window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = make_window()
    assert w.datasets.count() == 2 and w.delete_all_btn.isEnabled()
    w._delete_dataset()
    assert w.datasets.count() == 1 and w.current is not None
    w._delete_all()
    assert w.datasets.count() == 0 and w.current is None
    assert not w.delete_btn.isEnabled() and not w.delete_all_act.isEnabled()
    assert "Nothing saved yet" in w.storage_info.text()


def test_background_mood_follows_the_dataset_and_can_be_turned_off(make_window):
    from subsentiment.gui import theme
    assert theme.mood_score(3, 1, 4) == 1.0 and theme.mood_score(1, 3, 10) == -0.4 and theme.mood_score(0, 0, 0) == 0
    w = make_window()
    w._show_dataset("Python")  # one positive post; the window isn't shown, so the tint jumps straight there
    assert (w.mood.state.x(), w.mood.state.y()) == (1.0, 1.0)
    w._show_dataset("")
    assert w.mood.state.y() == 0.0  # no data: the plain backdrop
    w.mood_act.setChecked(False)
    w._show_dataset("Python")
    assert w.mood.state.y() == 0.0
    w.close()
    assert make_window().mood_act.isChecked() is False


def test_finish_text_says_what_the_fetch_did():
    req = mw.collect.FetchRequest("python")
    r = mw.collect.FetchResult("Python", 40, 900, False, updated=12, already=3, comment_errors=2)
    text = mw.finish_text(r, req).replace("\u00a0", " ")
    assert text == ("Added 40 new posts and 900 comments from r/Python. Updated scores on 12 saved posts. "
                    "3 top posts were already saved. Comments couldn't be fetched for 2 posts; "
                    "fetch again later to fill them in.")
    assert mw.finish_text(mw.collect.FetchResult("Python", 0, 0, False), req).startswith("No new posts found")


def test_window_fits_a_150_percent_screen():
    class Screen:
        def availableGeometry(self):
            from PySide6.QtCore import QRect
            return QRect(0, 0, 1280, 768)  # 1920x1200 at 150%, less the taskbar
    size = mw.fit_to_screen(mw.QSize(1320, 860), Screen())
    assert size.width() <= 1280 and size.height() <= 710
    assert mw.fit_to_screen(mw.QSize(1320, 860), None) == mw.QSize(1320, 860)


def test_rows_added_during_a_fetch_are_appended_and_fade_in():
    from subsentiment.gui.models import FADE_SECONDS, POST_COLUMNS, RowsModel
    now = [0.0]
    m = RowsModel(POST_COLUMNS, clock=lambda: now[0])
    row = lambda i: {"id": f"p{i}", "sentiment": 0.1, "label": "neutral", "title": "t", "selftext": "",  # noqa: E731
                     "score": 1, "num_comments": 1, "author": "a", "created_utc": 0.0}
    m.set_rows([row(1)])
    inserted = []
    m.rowsInserted.connect(lambda parent, a, b: inserted.append((a, b)))
    m.set_rows([row(2), row(1)], animate=True)
    assert inserted == [(1, 1)] and [r["id"] for r in m.rows] == ["p1", "p2"]
    fg = mw.Qt.ItemDataRole.ForegroundRole
    assert m.data(m.index(0, 0), fg) is None  # the old row doesn't fade
    assert m.data(m.index(1, 0), fg).alphaF() < 0.3
    now[0] = FADE_SECONDS
    m._tick()
    assert m.data(m.index(1, 0), fg) is None
    m.set_rows([row(3)], animate=True)  # not a superset (another dataset): plain reload
    assert [r["id"] for r in m.rows] == ["p3"]


def test_coverage_window_lists_days_and_downloads_through_the_fetch_job(make_window, monkeypatch):
    from subsentiment.gui import coverage

    def count(self, after, before):  # no network or thread: the archive "has" 5 posts every day
        self._on_counts(self.subreddit, {d: 5 for d in range(int(after), int(before), 86400)})
    monkeypatch.setattr(coverage.CoverageWindow, "_count", count)
    w = make_window()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.range.setCurrentIndex(w.range.findData("custom"))
    w.date_from.setDate(QDate(2023, 11, 13))  # the saved post is from 14 Nov 2023 (UTC)
    w.date_to.setDate(QDate(2023, 11, 15))
    started = []
    monkeypatch.setattr(w, "_launch", lambda req, msg: started.append(req))
    w._show_coverage()
    c = w.coverage
    c.refresh_saved()
    day = 1_699_920_000  # 14 Nov 2023 00:00 UTC
    assert day in c.days and c.days == sorted(c.days, reverse=True)
    row = c.days.index(day)
    cell = c.day_table.cellWidget(row, 1)
    assert cell.main.text() == "1 of 5" and cell.side.text() == "20%"
    c.day_table.cellWidget(row, 2).findChild(mw.QPushButton).click()
    req = started[-1]
    assert (req.after, req.before, req.everything, req.subreddit) == (day, day + 86400, True, "Python")
    assert req.limit == 4 + coverage.SLACK

    c.day_table.selectRow(row)
    QApplication.processEvents()
    assert c.post_table.rowCount() == 1  # its one post, with 0 comments counted, so nothing to fetch
    assert not c.post_table.cellWidget(0, 2).findChild(mw.QPushButton).isEnabled()
    c.close()


def test_archive_counts_arrive_on_the_gui_thread(make_window, monkeypatch):
    import threading

    from subsentiment.gui import coverage
    seen = {}

    class Counts(coverage.CountWorker):
        def run(self):
            self.finished.emit(self.args[0], {1_699_920_000: 5})
    monkeypatch.setattr(coverage, "CountWorker", Counts)

    class Totals(dict):  # notes which thread stores the counts
        def setdefault(self, *a):
            seen["thread"] = threading.current_thread()
            return super().setdefault(*a)
    w = make_window()
    w.datasets.setCurrentRow(0)
    c = coverage.CoverageWindow(w)
    c.totals = Totals()
    c.subreddit = "Python"
    c._count(1_699_920_000, 1_699_920_000 + 86400)
    c._count_thread.wait(5000)
    for _ in range(20):
        QApplication.processEvents()
    assert seen["thread"] is threading.main_thread()
    assert c.totals["python"] == {1_699_920_000: 5} and c._count_thread is None
    c.close()


def test_today_shows_what_is_saved_so_far_without_a_total(make_window, monkeypatch):
    from subsentiment.gui import coverage

    def count(self, after, before):  # the archive reports 0 for a day that has barely started
        self._on_counts(self.subreddit, {d: 0 for d in range(int(after), int(before), 86400)})
    monkeypatch.setattr(coverage.CoverageWindow, "_count", count)
    w = make_window()
    today = coverage.today()
    s = w.store
    s.save_post(Post("t1", "Python", "fresh", "", "a", None, None, None, today + 60, "https://r", ""), 0.0, "neutral")
    s.commit()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.range.setCurrentIndex(w.range.findData("week"))
    started = []
    monkeypatch.setattr(w, "_launch", lambda req, msg: started.append((req, msg)))
    w._show_coverage()
    c = w.coverage
    c.refresh_saved()
    row = c.days.index(today)
    cell = c.day_table.cellWidget(row, 1)
    assert (cell.main.text(), cell.side.text()) == ("1 saved so far", "Today")
    assert "of" not in cell.main.text() and "%" not in cell.side.text()
    button = c.day_table.cellWidget(row, 2).findChild(mw.QPushButton)
    assert button.text() == "Download all" and button.isEnabled()
    button.click()
    req, msg = started[-1]
    assert req.everything and req.after == today and req.limit == coverage.MAX_DAY_POSTS
    assert "so far today" in msg
    c.close()
