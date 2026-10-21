"""The span of saved days every page counts, chosen on the timeline (its range buttons, zoom, drag selection
and navigator): what it filters, live, and what it remembers. It replaced the header's Days picker."""
import csv
import os
import time
from datetime import date

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from subsentiment.gui import days, motion  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.reddit import Comment, Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

DAY = 86400
D1, D2, D3 = (days.from_date(date(2026, 10, d)) for d in (5, 6, 9))  # 7 and 8 Oct have nothing saved


def settings_in(tmp_path, name="days-test"):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    return QSettings()


@pytest.fixture
def settings(tmp_path):
    yield settings_in(tmp_path)
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


@pytest.fixture
def window(tmp_path, monkeypatch):
    settings_in(tmp_path, tmp_path.name)
    QSettings().setValue("updates/auto", False)
    db = tmp_path / "d.db"
    s = Store(db)
    for i, (day, label, score) in enumerate([(D1, "positive", 0.6), (D1, "positive", 0.4), (D2, "negative", -0.5),
                                             (D3, "neutral", 0.0), (D3, "negative", -0.7)]):
        s.save_post(Post(f"p{i}", "Python", f"title {i}", "", "a", 1, 0.9, 1, day + 3600 * (i + 1),
                         f"https://r/{i}", ""), score, label)
        s.save_comment(Comment(f"c{i}", f"p{i}", "Python", f"t3_p{i}", "b", f"reply {i}", 1, day + 7200 * (i + 1), 0,
                               "u"), score, label)
    s.save_post(Post("r0", "rust", "crab", "", "a", 1, 0.9, 0, D2 + 60, "https://r/r0", ""), 0.5, "positive")
    s.commit()
    s.close()
    windows = []

    def make():
        w = mw.MainWindow(str(db))
        motion.set_enabled(False)  # each change lands at once
        w.resize(1280, 721)
        w.show()
        w._refresh_datasets(select="Python")
        windows.append(w)
        QApplication.processEvents()
        return w
    yield make
    for w in windows:
        w.close()
    motion.set_enabled(True)
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def pump(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def drag(tl, lo: float, hi: float) -> None:
    """Drag across the plot from ``lo`` to ``hi``, as a user zooms in to some days."""
    plot, _ = tl._areas()
    a, b, _ = tl._screen
    x0, x1 = (round(tl._x(t, a, b, plot)) for t in (lo, hi))
    y = round(plot.center().y())
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(x0, y))
    QTest.mouseMove(tl, QPoint((x0 + x1) // 2, y))
    QTest.mouseMove(tl, QPoint(x1, y))
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(x1, y))


def settle(w) -> None:
    """Until the tables follow the span: its settling delay has run out and the steps after it are done. A fixed
    wait past the delay missed the steps on a busy runner (3 posts shown of 5 on Linux CI)."""
    end = time.monotonic() + 5
    while (w._span_settled.isActive() or w._span_slices.running()) and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


def tiles(w) -> int:
    return int(w.overview.tiles["count"].value.text())


def test_there_is_no_days_picker_in_the_header(window):
    w = window()
    assert not hasattr(w, "days_pick") and not hasattr(w, "presets")
    assert w.overview.timeline.toolbar.isVisible()  # the timeline's own range buttons stay


def test_every_view_counts_the_days_the_timeline_shows(window, tmp_path, monkeypatch):
    w = window()
    tl = w.overview.timeline
    assert w.span is None and tiles(w) == 10 and len(w.posts) == 5
    drag(tl, D1 + 0.1 * DAY, D2 + 0.9 * DAY)  # 5 and 6 Oct, held to the closest zoom: 5 to 8 Oct
    assert tl.view is not None and tl.view[0] < D2 and D2 + DAY < tl.view[1] <= D3
    # The tiles and distribution follow at once, before the tables do.
    assert tiles(w) == 6 and sum(w.overview.distribution._counts.values()) == 6
    assert w.overview.distribution._counts == {"negative": 2, "neutral": 0, "positive": 4}
    assert w.overview.tiles["mean"].value.text() == "+0.167"
    assert [d for d, _, _ in tl.points] == [D1, D2, D3]  # the timeline still has every day, to zoom back out
    settle(w)
    assert len(w.posts) == 3 and len(w.comments) == 3
    assert w.posts_tab.model.rowCount() == 3 and w.comments_tab.model.rowCount() == 3
    assert w.subheading.full_text().endswith("· 5 – 7 Oct · 3\xa0posts · 3\xa0comments")

    out = tmp_path / "posts.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    w._export("posts")
    assert sorted(r["id"] for r in csv.DictReader(open(out, encoding="utf-8-sig"))) == ["p0", "p1", "p2"]
    assert "from 5 Oct 2026 to " in w.statusBar().currentMessage()

    w._refresh_datasets(select="rust")  # another subreddit has its own span: every day
    assert w.span is None and tl.view is None and len(w.posts) == 1 and tiles(w) == 1
    w._refresh_datasets(select="Python")
    assert w.span == tl.view and tl.view is not None and len(w.posts) == 3 and tiles(w) == 6

    QTest.mouseDClick(tl, Qt.MouseButton.LeftButton, pos=tl._areas()[0].center().toPoint())  # every day again
    assert w.span is None and tiles(w) == 10
    settle(w)
    assert len(w.posts) == 5


def test_the_timeline_page_and_the_overview_share_one_span(window):
    w = window()
    w.show_page("timeline")
    QApplication.processEvents()
    page = w.timeline_page.body
    drag(page, D2 + 0.5 * DAY, D3)
    assert page.view is not None and w.overview.timeline.view == page.view == w.span
    assert tiles(w) < 10
    page.zoom_preset("All")
    assert w.overview.timeline.view is None and tiles(w) == 10
    w.overview.timeline.set_view(D1, D1 + 3 * DAY)
    w.show_page("timeline")
    assert page.view == (D1, D1 + 3 * DAY)


def test_the_span_survives_a_restart_and_every_day_forgets_it(window):
    w = window()
    w.overview.timeline.set_view(D2, D2 + 4 * DAY)  # 6 to 9 Oct
    assert tiles(w) == 6
    w.close()

    again = window()
    assert again.span == again.overview.timeline.view == (D2, D2 + 4 * DAY)
    assert tiles(again) == 6 and len(again.posts) == 3
    again.overview.timeline.zoom_preset("All")
    assert again.span is None and not again.settings.contains(days.settings_keys("Python")[0])


def test_a_range_button_is_remembered_by_name(settings):
    s = settings
    days.save_range(s, "Python", (D1, D3), "1w")
    assert days.load_range(s, "python") == (None, "1w")  # names are case-insensitive, like the store's
    days.save_range(s, "Python", (D1, D3), None)
    assert days.load_range(s, "Python") == ((D1, D3), None)
    assert days.load_range(s, "rust") == (None, None)
    days.save_range(s, "Python", None, "All")
    assert days.load_range(s, "Python") == (None, None) and not s.contains(days.settings_keys("Python")[0])


def test_days_picked_in_earlier_versions_open_as_the_span_from_the_first_to_the_last(settings):
    s = settings
    key, old = days.settings_keys("Python")
    s.setValue(old, "2026-10-05,2026-10-09")  # what the Days picker stored
    assert days.load_range(s, "Python") == ((D1, D3 + DAY), None)
    s.setValue(old, "")  # "No days" picked: every day now, as the timeline can't show none
    assert days.load_range(s, "Python") == (None, None)
    s.setValue(old, "2026-10-06")
    days.save_range(s, "Python", (D1, D3), None)  # a new span replaces it
    assert not s.contains(old) and days.load_range(s, "Python") == ((D1, D3), None)


def test_an_old_days_choice_is_shown_on_the_timeline(window):
    QSettings().setValue(days.settings_keys("Python")[1], "2026-10-05,2026-10-06")
    w = window()
    assert w.overview.timeline.view == (D1, D1 + 3 * DAY)  # 5 and 6 Oct, widened to the closest zoom
    assert tiles(w) == 6


def test_deleting_a_subreddit_forgets_its_span(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = window()
    w.overview.timeline.set_view(D1, D1 + 3 * DAY)
    assert w.settings.contains(days.settings_keys("Python")[0])
    w._delete_dataset()
    assert not any(w.settings.contains(k) for k in days.settings_keys("Python"))
    w._refresh_datasets(select="rust")
    QSettings().setValue(days.settings_keys("rust")[1], "2026-10-06")
    w._delete_all()
    assert not {days.SETTINGS_GROUP, days.RANGE_GROUP} & set(w.settings.childGroups())


def test_keep_filters_rows_by_the_days_they_were_created_on():
    rows = [{"created_utc": None}, {"created_utc": D1 + 10}, {"created_utc": D2 + DAY - 1}, {"created_utc": D3}]
    assert days.keep(rows, None) == rows and days.keep(rows, None) is not rows
    assert days.keep(rows, days.whole((D1 + 0.2 * DAY, D2 + 0.7 * DAY))) == rows[1:3]
    assert days.keep(rows, days.whole((D1 + 0.6 * DAY, D2 + 0.7 * DAY))) == rows[2:3]  # 5 Oct's point is out of view
    assert days.keep(rows, days.whole((D2 + DAY, D3))) == []


def test_the_span_is_named_briefly():
    assert days.span_text((D2, D2 + DAY)) == "6 Oct"
    assert days.span_text((D1, D3 + DAY)) == "5 – 9 Oct"
    assert days.span_text((D1 - 7 * DAY, D1)) == "28 Sep – 4 Oct"
    new_year = days.from_date(date(2026, 1, 1))
    assert days.span_text((new_year - 2 * DAY, new_year + DAY)) == "30 Dec 2025 – 1 Jan 2026"


def test_a_day_counts_when_its_point_is_in_view():
    assert days.whole(None) is None
    assert days.whole((D1, D1 + 3 * DAY)) == (D1, D1 + 3 * DAY)  # whole days, as the range buttons show them
    assert days.whole((D1 + 0.4 * DAY, D1 + 3.6 * DAY)) == (D1, D1 + 4 * DAY)
    assert days.whole((D1 + 0.6 * DAY, D1 + 3.4 * DAY)) == (D1 + DAY, D1 + 3 * DAY)


def test_switching_the_scope_leaves_the_remembered_span_alone(window):
    w = window()
    w.overview.timeline.set_view(D2, D2 + 3 * DAY)
    key = days.settings_keys("Python")[0]
    saved = w.settings.value(key)
    w.scope.setCurrentText("Posts only")
    w.scope.setCurrentText("Posts and comments")
    assert w.settings.value(key) == saved


def test_rows_without_a_date_are_left_out_of_the_counts():
    from subsentiment.analysis import Analysis
    a = Analysis([{"created_utc": None, "sentiment": 0.5, "label": "positive"},
                  {"created_utc": D1, "sentiment": -0.5, "label": "negative"}])
    assert a.count == 1 and a.mean == -0.5
