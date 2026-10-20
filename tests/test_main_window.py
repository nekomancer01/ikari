"""The fetch form and saved-data list survive a restart; the delete actions clear the store."""
import os
import time
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QDate, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui.fetch_form import range_bounds  # noqa: E402
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
    w.fetch_form.subreddit.setText("r/AskScience")
    w.fetch_form.query.setText("black holes")
    w.fetch_form.date_from.setDate(QDate(2026, 3, 1))  # editing a date switches to a custom range
    w.fetch_form.date_to.setDate(QDate(2026, 3, 31))
    w.fetch_form.include_comments.setChecked(False)
    w.fetch_form.comments_per_post.setValue(20)
    assert not w.fetch_form.refresh_scores.isChecked()  # off by default
    w.fetch_form.refresh_scores.setChecked(True)
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.show_page("posts")
    w.close()

    again = make_window()
    assert again.fetch_form.subreddit.text() == "r/AskScience" and again.fetch_form.query.text() == "black holes"
    assert again.fetch_form.range.currentData() == "custom"
    form = again.fetch_form
    assert (form.date_from.date(), form.date_to.date()) == (QDate(2026, 3, 1), QDate(2026, 3, 31))
    assert (again.fetch_form.include_comments.isChecked(), again.fetch_form.comments_per_post.value()) == (False, 20)
    assert not again.fetch_form.comments_per_post.isEnabled()
    assert again.fetch_form.refresh_scores.isChecked()
    assert again.current == "Python" and again.page() == "posts"


def test_comments_are_fetched_by_default_and_presets_fill_the_dates(make_window):
    w = make_window()
    assert w.fetch_form.include_comments.isChecked() and w.fetch_form.range.currentData() == "week"
    today = QDate.currentDate()
    assert (w.fetch_form.date_from.date(), w.fetch_form.date_to.date()) == (today.addDays(-7), today)
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("month"))
    assert w.fetch_form.date_from.date() == today.addDays(-30) and w.fetch_form.range.currentData() == "month"


def test_range_bounds():
    now = datetime(2026, 10, 19, 12, 0)
    assert range_bounds("day", QDate(), QDate(), now) == ((now - timedelta(days=1)).timestamp(), None)
    after, before = range_bounds("custom", QDate(2026, 3, 31), QDate(2026, 3, 1), now)  # reversed is fine
    assert after == datetime(2026, 3, 1).timestamp() and before == datetime(2026, 4, 1).timestamp()


def test_fetch_builds_a_date_range_request(make_window, monkeypatch):
    sent = []
    monkeypatch.setattr(mw, "FetchWorker", lambda req, db: sent.append(req) or (_ for _ in ()).throw(RuntimeError))
    w = make_window()
    w.fetch_form.subreddit.setText("python")
    w.fetch_form.date_from.setDate(QDate(2026, 3, 1))
    w.fetch_form.date_to.setDate(QDate(2026, 3, 2))
    with pytest.raises(RuntimeError):
        w._start_fetch()
    (req,) = sent
    assert (req.after, req.before) == (datetime(2026, 3, 1).timestamp(), datetime(2026, 3, 3).timestamp())
    assert req.include_comments


def test_invalid_subreddit_is_explained_without_starting_a_fetch(make_window):
    w = make_window()
    w.fetch_form.subreddit.setText("not a sub")
    w._start_fetch()
    assert not w.fetch_job.busy and "isn't a valid subreddit name" in w.progress_msg.text()


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


def test_the_window_says_which_database_it_has_open(make_window, tmp_path, monkeypatch):
    # Started under another home (sudo, a Flatpak IDE) the app opens a different, empty database, and nothing
    # on screen said where the one it had open was.
    opened = []
    monkeypatch.setattr(mw.QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    w = make_window()
    assert str(tmp_path / "w.db") in w.storage_info.toolTip()
    assert w.data_folder_act.statusTip() == str(tmp_path / "w.db")
    w.data_folder_act.trigger()
    assert [os.path.normpath(p) for p in opened] == [str(tmp_path)]


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


def test_background_mood_uses_the_chart_colors_in_both_themes(make_window):
    from PySide6.QtGui import QColor
    from subsentiment.gui import charts, theme
    make_window()
    try:
        for mode in ("light", "dark"):
            theme._mode = mode
            for score, key in ((1.0, "positive"), (-1.0, "negative")):
                wash, weight = theme.mood_color(score)
                assert weight == 1.0 and wash.rgb() == QColor(charts.palette()[key]).rgb()
    finally:
        theme._mode = "light"


def test_finish_text_says_what_the_fetch_did():
    req = mw.collect.FetchRequest("python")
    r = mw.collect.FetchResult("Python", 40, 900, False, updated=12, already=3, comment_errors=2)
    text = mw.finish_text(r, req).replace("\u00a0", " ")
    assert text == ("Added 40 new posts and 900 comments from r/Python. Updated scores on 12 saved posts. "
                    "3 top posts were already saved. Comments couldn't be fetched for 2 posts; "
                    "fetch again later to fill them in.")
    assert mw.finish_text(mw.collect.FetchResult("Python", 0, 0, False), req).startswith("No new posts found")
    gap = mw.collect.FetchResult("Python", 40, 0, False, timed_out=True)
    assert "timed out on part of the range" in mw.finish_text(gap, req)
    assert "timed out" in mw.finish_text(mw.collect.FetchResult("Python", 0, 0, False, timed_out=True), req)


def test_window_fits_a_150_percent_screen():
    class Screen:
        def availableGeometry(self):
            from PySide6.QtCore import QRect
            return QRect(0, 0, 1280, 768)  # 1920x1200 at 150%, less the taskbar
    size = mw.fit_to_screen(mw.QSize(1320, 860), Screen())
    assert size.width() <= 1280 and size.height() <= 710
    assert mw.fit_to_screen(mw.QSize(1320, 860), None) == mw.QSize(1320, 860)


def test_rows_added_during_a_fetch_are_appended_and_fade_in():
    from subsentiment.gui.tables import FADE_SECONDS, POST_COLUMNS, RowsModel
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


def test_append_rows_shows_each_new_row_fading_in_and_skips_ones_already_shown():
    from subsentiment.gui.tables import POST_COLUMNS, RowsModel
    now = [0.0]
    m = RowsModel(POST_COLUMNS, clock=lambda: now[0])
    row = lambda i: {"id": f"p{i}", "sentiment": 0.1, "label": "neutral", "title": "t", "selftext": "",  # noqa: E731
                     "score": 1, "num_comments": 1, "author": "a", "created_utc": 0.0}
    m.set_rows([row(1)])
    assert m.append_rows([row(1), row(2)]) == 1 and m.append_rows([row(2)]) == 0
    fg = mw.Qt.ItemDataRole.ForegroundRole
    assert m.data(m.index(0, 0), fg) is None and m.data(m.index(1, 0), fg).alphaF() < 0.3


def test_rows_saved_by_a_running_fetch_are_dripped_into_the_tables(make_window):
    w = make_window()
    w.fetch_job.qthread = object()  # a fetch is "running"
    w.request = mw.collect.FetchRequest("Python")
    w._since = 0.0
    w._show_dataset("Python")
    before = len(w.posts)
    row = lambda i: {"id": f"new{i}", "sentiment": 0.1, "label": "neutral", "title": "t", "selftext": "",  # noqa: E731
                     "score": None, "num_comments": None, "author": "a", "created_utc": 1.0, "fetched_at": 1.0}
    w.posts_tab.add_rows([row(1), row(2)])
    assert len(w.posts) == before + 2 and w.posts_tab.model.rowCount() == before + 2
    assert w.posts_tab.add_rows([row(2)]) == 0
    w.fetch_job.qthread = None


def test_coverage_window_lists_days_and_downloads_through_the_fetch_job(make_window, monkeypatch):
    from subsentiment.gui import coverage

    def count(self, after, before):  # no network or thread: the archive "has" 5 posts every day
        self._on_counts(self.subreddit, {d: 5 for d in range(int(after), int(before), 86400)})
    monkeypatch.setattr(coverage.CoverageWindow, "_count", count)
    w = make_window()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("custom"))
    w.fetch_form.date_from.setDate(QDate(2023, 11, 13))  # the saved post is from 14 Nov 2023 (UTC)
    w.fetch_form.date_to.setDate(QDate(2023, 11, 15))
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
    assert req.limit == coverage.MAX_DAY_POSTS  # everything; the day is complete only if the archive ran out

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
    end = time.monotonic() + 10  # the thread is wound down from the GUI thread, so keep handling events
    while (c.count_job.busy or "thread" not in seen) and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert seen["thread"] is threading.main_thread()
    assert c.totals["python"] == {1_699_920_000: 5} and not c.count_job.busy
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
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    started = []
    monkeypatch.setattr(w, "_launch", lambda req, msg: started.append((req, msg)))
    w._show_coverage()
    c = w.coverage
    c.refresh_saved()
    row = c.days.index(today)
    cell = c.day_table.cellWidget(row, 1)
    assert (cell.main.text(), cell.note.text()) == ("1 saved so far", "Today")
    assert "of" not in cell.main.text() and "%" not in cell.note.text()
    button = c.day_table.cellWidget(row, 2).findChild(mw.QPushButton)
    assert button.text() == "Download all" and button.isEnabled()
    button.click()
    req, msg = started[-1]
    assert req.everything and req.after == today and req.limit == coverage.MAX_DAY_POSTS
    assert "so far today" in msg
    c.close()


def test_today_and_counts_below_whats_saved_show_no_total(make_window, monkeypatch):
    from subsentiment.gui import coverage
    yesterday, older = coverage.today() - 86400, coverage.today() - 3 * 86400

    def count(self, after, before):  # 3 days ago the count is below what's saved
        self._on_counts(self.subreddit, {d: (1 if d == older else 9) for d in range(int(after), int(before), 86400)})
    monkeypatch.setattr(coverage.CoverageWindow, "_count", count)
    w = make_window()
    for day in (yesterday, older):
        for i in range(3):
            w.store.save_post(Post(f"y{day}{i}", "Python", "y", "", "a", None, None, None, day + 60 + i, "u", ""),
                              0, "neutral")
    w.store.commit()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    w._show_coverage()
    c = w.coverage
    c.refresh_saved()
    # The older day's count (1) can't be right; yesterday's is whole and shown.
    cell = c.day_table.cellWidget(c.days.index(older), 1)
    assert (cell.main.text(), cell.note.text()) == ("3 saved so far", "No usable count")
    cell = c.day_table.cellWidget(c.days.index(yesterday), 1)
    assert (cell.main.text(), cell.side.text()) == ("3 of 9", "33%")
    cell = c.day_table.cellWidget(c.days.index(older + 86400), 1)
    assert (cell.main.text(), cell.side.text()) == ("0 of 9", "0%")  # a settled day keeps its total
    c.close()


def test_one_row_is_counted_as_one_row(make_window):
    w = make_window()
    assert w.posts_tab.count.text() == "1 row"


def test_posts_without_a_score_yet_sort_below_every_real_score(make_window):
    from subsentiment.gui.tables import POST_COLUMNS, RowsModel, SORT_ROLE
    model = RowsModel(POST_COLUMNS)
    col = next(i for i, c in enumerate(POST_COLUMNS) if c[1] == "score")
    rows = [{"id": i, "score": s, "title": "t", "selftext": "", "label": "neutral"}
            for i, s in enumerate((-5, None, -1, 0))]
    model.set_rows(rows)
    keys = [model.data(model.index(r, col), SORT_ROLE) for r in range(4)]
    assert sorted(range(4), key=lambda r: keys[r]) == [1, 0, 2, 3]  # pending first, then -5, -1, 0


def test_an_export_that_cant_be_saved_says_so(make_window, monkeypatch, tmp_path):
    # On Windows the usual cause is the CSV still being open in Excel. The error used to escape the slot:
    # no message at all, and in the packaged app no console to show it either.
    w = make_window()
    target = tmp_path / "out.csv"
    monkeypatch.setattr(mw.QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))

    def locked(*a, **k):
        raise PermissionError(13, "Permission denied", str(target))
    monkeypatch.setattr(w.store, "export_csv", locked)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[1:3]))
    w._export("posts")
    assert warned and warned[0][0] == "Export failed"
    assert "close it and try again" in warned[0][1]


def test_bundled_fonts_are_weights_of_the_families_the_theme_asks_for():
    """Each bundled weight must belong to the family the theme names, or Qt (on Windows too) treats
    "Inter Medium" as a family of its own and draws medium and semibold text in a synthetic weight."""
    from PySide6.QtGui import QFontDatabase

    from subsentiment.gui import theme
    QApplication.instance() or QApplication([])
    files = sorted(theme.ASSETS.glob("*.otf"))
    assert files
    for path in files:
        families = QFontDatabase.applicationFontFamilies(QFontDatabase.addApplicationFont(str(path)))
        wanted = theme.DISPLAY_FAMILY if path.name.startswith("InterDisplay") else theme.UI_FAMILIES[0]
        assert wanted in families, f"{path.name} registers as {families}, not as a weight of {wanted}"


def test_each_rail_button_opens_its_page_and_the_last_one_is_reopened(make_window):
    w = make_window()
    for page in ("timeline", "words", "posts", "comments", "overview", "timeline"):
        w.rail.buttons[page].click()
        assert w.page() == page and w.pages.currentWidget() is w.page_widgets[page]
        assert w.scope.isVisibleTo(w) == (page in ("overview", "timeline", "words"))  # what it counts shows there
    assert w.timeline_page.body.points == w.overview.timeline.points != []  # the same days, on its own page
    w.close()
    assert make_window().page() == "timeline"


def test_the_old_tab_setting_opens_the_same_page(make_window):
    QSettings().setValue("ui/tab", 2)  # Comments, from before the rail
    assert make_window().page() == "comments"


def test_saved_drawer_switches_subreddit_and_escape_closes_it(make_window):
    w = make_window()
    assert w.drawer.isHidden()  # data is saved: the page shows, no drawer
    w.rail.buttons["saved"].click()
    assert w._drawer_key() == "saved" and w.datasets.isVisibleTo(w)
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/rust"))
    assert w.current == "rust" and w.heading.text() == "r/rust" and w._drawer_key() == "saved"
    w.rail.buttons["fetch"].click()  # one drawer at a time
    assert w._drawer_key() == "fetch" and not w.rail.buttons["saved"].isChecked()
    QTest.keyClick(w.fetch_form.query, Qt.Key.Key_Escape)
    assert w.drawer.isHidden() and not w.rail.buttons["fetch"].isChecked()


def test_the_drawer_floats_over_the_page_on_a_narrow_window(make_window):
    w = make_window()
    w.show()
    w.resize(1, 600)  # as narrow as the page lets it go, with no drawer open
    QApplication.processEvents()
    narrowest = w.width()
    root = w.centralWidget().layout()
    m = root.contentsMargins()
    # Wide enough for the drawer beside the page, measured: Windows' fonts make the page wider than here.
    wide = (m.left() + w.rail.sizeHint().width() + 2 * root.spacing() + mw.DRAWER_WIDTH
            + w.content.minimumSizeHint().width() + m.right() + 40)
    w.resize(wide, 720)
    w.set_drawer("fetch")
    QApplication.processEvents()
    assert root.indexOf(w.drawer) >= 0  # room for both: the page moves over
    assert w.drawer.geometry().right() < w.content.geometry().left()
    for _ in range(3):  # the first lets the drawer out of the layout; the window's minimum catches up after
        w.resize(1, 600)
        QApplication.processEvents()
    assert root.indexOf(w.drawer) < 0 and w.drawer.width() == mw.DRAWER_WIDTH
    assert w.content.width() >= w.content.minimumSizeHint().width() and w.width() <= narrowest
    w.resize(wide, 720)
    QApplication.processEvents()
    assert root.indexOf(w.drawer) >= 0


def test_the_timelines_range_buttons_keep_the_newest_saved_days_on_every_page(make_window, tmp_path):
    s = Store(tmp_path / "w.db")
    newest = 1_700_000_000.0
    for d in range(40):  # one post a day for 40 days, the newest on the saved post's day
        s.save_post(Post(f"d{d}", "Python", "ok", "", "a", 1, 0.9, 0, newest - d * 86400, "https://r", ""), 0.1,
                    "neutral")
    s.commit()
    s.close()
    w = make_window()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    buttons = w.overview.timeline.presets
    assert buttons["All"].isChecked() and len(w.posts) == 41
    buttons["1w"].click()
    w._apply_span()  # what the tables do once the span settles
    assert len(w.posts) == 8 and w.overview.tiles["count"].value.text() == "8"  # the newest day has two posts
    assert w._preset == "1w" and buttons["1w"].isChecked()
    buttons["1m"].click()
    w._apply_span()
    assert len(w.posts) == 31 and w._preset == "1m"
    buttons["All"].click()
    w._apply_span()
    assert w.span is None and len(w.posts) == 41
    buttons["1w"].click()
    w.close()
    again = make_window()  # the span is kept with the subreddit
    assert again.current == "Python" and len(again.posts) == 8 and again.overview.timeline.presets["1w"].isChecked()


@pytest.mark.parametrize("counts", [(65_432, 412_837, 98_765), (1_234_567, 7_654_321, 2_345_678)])
def test_the_distribution_shows_big_counts_whole(counts):
    """The count column must fit the count as painted. It was sized from the display font alone, but
    painting adds the app font's tabular figures, which are wider, so "4,137" was drawn as ",137"."""
    from PySide6.QtGui import QColor, QFontMetricsF, QPainter

    from subsentiment.gui import motion, theme
    from subsentiment.gui.charts import DistributionChart
    app = QApplication.instance() or QApplication([])
    before = app.font()
    theme.load_fonts(app)
    theme.apply(app, "light")
    motion.set_enabled(False)
    try:
        chart = DistributionChart()
        chart.resize(520, 330)
        chart.set_counts(dict(zip(DistributionChart.CATEGORIES, counts)))
        image = chart.grab().toImage()
        dpr = image.devicePixelRatio()
        p = QPainter(image)
        p.setFont(theme.display_font(chart.font().pointSizeF() + 0.5, 600))
        metrics = QFontMetricsF(p.fontMetrics())
        p.end()
        _, rows = chart._layout()
        for n, row in zip(counts, rows):
            # Count ink is the near-black gray right of the middle: names are on the left, shares muted, bars colored.
            ys = range(int(row.top() * dpr) + 2, int(row.bottom() * dpr) - 2)
            ink = [x for x in range(image.width() // 2, image.width())
                   if any((c := QColor.fromRgba(image.pixel(x, y))).alpha() > 200 and c.lightness() < 80 and c.hslSaturation() < 40 for y in ys)]
            assert ink, f"{n:,} not drawn"
            assert (ink[-1] - ink[0] + 1) / dpr >= metrics.horizontalAdvance(f"{n:,}") - 4, f"{n:,} is cut off"
    finally:
        motion.set_enabled(True)
        app.setFont(before)


def test_date_popup_shows_whole_month_and_picks_a_day(make_window):
    """The From field's calendar pops up as a card holding all six weeks (the stock one cut the last row
    off), and clicking a day picks it and switches to custom dates."""
    from PySide6.QtCore import QPoint, QRect, Qt
    from PySide6.QtTest import QTest
    from subsentiment.gui.widgets import Calendar
    w = make_window()
    w.show()
    w.set_drawer("fetch")  # the form lives in the Fetch drawer
    edit = w.fetch_form.date_from
    w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData("week"))
    if not edit.isVisible():
        pytest.skip("the date field isn't on screen in this layout")
    QTest.mouseClick(edit, Qt.MouseButton.LeftButton, pos=QPoint(edit.width() - 10, edit.height() // 2))
    QApplication.processEvents()
    cal = edit.calendarWidget()
    popup = cal.window()
    assert isinstance(cal, Calendar) and popup.isVisible()
    view = cal.view
    last = QRect(view.columnViewportPosition(6), view.rowViewportPosition(5), view.columnWidth(6), view.rowHeight(5))
    shown = QRect(view.viewport().mapTo(popup, last.topLeft()), last.size())
    assert popup.rect().contains(shown) and view.viewport().rect().contains(last)
    model = view.model()
    cell = next(model.index(r, c) for r in range(2) for c in range(7) if model.index(r, c).data() == 1)
    first = QDate(cal.yearShown(), cal.monthShown(), 1)  # From is a week back; the 1st of its month is earlier
    QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=view.visualRect(cell).center())
    QApplication.processEvents()
    assert edit.date() == first
    assert not popup.isVisible()
    assert w.fetch_form.range.currentData() == "custom"


def test_import_database_adds_its_subreddits_to_the_list(make_window, tmp_path, monkeypatch):
    other = Store(tmp_path / "other.db")
    other.save_post(Post("q1", "Deltarune", "great", "", "a", 1, 0.9, 0, 1_700_000_000.0, "https://r", ""), 0.6,
                    "positive")
    other.commit()
    other.close()
    monkeypatch.setattr(mw.QFileDialog, "getOpenFileName", lambda *a, **k: (str(tmp_path / "other.db"), ""))
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a))
    w = make_window()
    assert w.import_act.isEnabled()
    w.import_act.trigger()
    assert sorted(w.datasets.item(i).text() for i in range(w.datasets.count())) == ["r/Deltarune", "r/Python", "r/rust"]
    assert "Imported 1\xa0new post" in w.statusBar().currentMessage()
    (tmp_path / "other.db").write_bytes(b"junk" * 1000)
    w.import_act.trigger()
    assert len(warnings) == 1 and w.datasets.count() == 3


def test_score_filter_hides_low_posts_and_their_comments_everywhere_and_is_remembered(make_window, tmp_path):
    from subsentiment.reddit import Comment
    s = Store(tmp_path / "w.db")
    ts = 1_700_000_000.0
    for pid, score in (("low", 3), ("mid", 40), ("high", 900), ("pending", None)):
        s.save_post(Post(pid, "Python", pid, "", "a", score, 0.9, 1, ts, f"https://r/{pid}/", ""), 0.5, "positive")
        s.save_comment(Comment(f"c-{pid}", pid, "Python", f"t3_{pid}", "b", "hi", 1, ts, 0, ""), -0.5, "negative")
    s.commit()
    s.close()
    w = make_window()
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    assert w.score_pick.currentText() == "Any score" and len(w.posts) == 5 and len(w.comments) == 4
    w.score_pick.setCurrentIndex(w.score_pick.findText("Score 10+"))
    # p0 (score 1) and "low" go, with low's comment; a score the archive hasn't filled in yet isn't held against it.
    assert sorted(r["id"] for r in w.posts) == ["high", "mid", "pending"]
    assert sorted(r["id"] for r in w.comments) == ["c-high", "c-mid", "c-pending"]
    assert w.posts_tab.model.rowCount() == 3 and w.comments_tab.model.rowCount() == 3
    assert w._counted.count == 6  # the Overview and Timeline count what the tables show
    assert "3\xa0posts" in w.subheading.text()
    out = tmp_path / "p.csv"
    assert w.store.export_csv("Python", "comments", out, min_score=w.min_score) == 3

    # A running fetch's rows go through the same filter as they arrive.
    w.fetch_job.qthread = object()
    w.request, w._since = mw.collect.FetchRequest("Python"), time.time()
    w._read_to = {"posts": w._since, "comments": w._since}
    w.store.save_post(Post("new-low", "Python", "n", "", "a", 2, 0.9, 1, ts, "https://r/n/", ""), 0.5, "positive")
    w.store.save_post(Post("new-high", "Python", "n", "", "a", 50, 0.9, 1, ts, "https://r/h/", ""), 0.5, "positive")
    w.store.save_comment(Comment("c-new-low", "new-low", "Python", "t3_new-low", "b", "x", 1, ts, 0, ""), 0.1, "neutral")
    w.store.commit()
    w._queue_new_rows()
    w._flush_rows()
    w.fetch_job.qthread = None
    assert sorted(r["id"] for r in w.posts_tab.all_rows) == ["high", "mid", "new-high", "pending"]
    assert "c-new-low" not in {r["id"] for r in w.comments_tab.all_rows}
    # The finished fetch rescored "high" below the filter: it leaves every page, and so does its comment.
    w.store.save_post(Post("high", "Python", "high", "", "a", 4, 0.9, 1, ts, "https://r/high/", ""), 0.5, "positive")
    w.store.commit()
    w._merge_fetched()
    assert "high" not in {r["id"] for r in w.posts_tab.all_rows} and "high" not in {r["id"] for r in w.posts}
    assert "c-high" not in {r["id"] for r in w.comments_tab.all_rows}
    w.close()

    again = make_window()
    assert again.score_pick.currentText() == "Score 10+" and len(again.posts) == 3
    again.score_pick.setCurrentIndex(again.score_pick.findText("Any score"))
    assert len(again.posts) == 7 and len(again.comments) == 5


def test_short_titles_leave_room_for_the_comments_column(make_window):
    """QA on a2d5074: at 1920x1200 150% with the Saved drawer open, the Posts table hid its Comments column
    although the titles were short and left half the table empty. The title column kept 40% of the width
    whatever its titles needed."""
    from subsentiment.gui import theme
    from subsentiment.gui.tables import POST_COLUMNS, TablePage
    app = QApplication.instance() or QApplication([])
    before = app.font()
    make_window()
    theme.load_fonts(app)  # Inter, as the app draws with: its widths decide what fits
    theme.apply(app, "light")
    try:
        page = TablePage(POST_COLUMNS, stretch_col=2)
        page.resize(795, 600)  # the table's width there, in logical pixels
        page.show()
        row = {"id": "p1", "sentiment": -0.957, "label": "negative", "title": "terrible bug #188", "selftext": "",
               "score": 134, "num_comments": 12, "author": "someone", "created_utc": 1_700_000_000.0}
        page.load([row])
        QApplication.processEvents()
        header = page.view.horizontalHeader()
        keys = [key for _, key, _ in POST_COLUMNS]
        assert not header.isSectionHidden(keys.index("num_comments"))
        assert header.sectionSize(keys.index("title")) >= page.view.viewport().width() // 4
        page.resize(600, 600)  # a long title on a narrow table still keeps a third of it before the counts
        page.load([dict(row, title="A much longer title that needs a good part of the table to be read " * 2)])
        QApplication.processEvents()
        assert header.sectionSize(keys.index("title")) >= page.view.viewport().width() // 3 - 1
        page.close()
    finally:
        app.setFont(before)
