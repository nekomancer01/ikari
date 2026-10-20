"""Choosing which saved days the analysis counts: the header's day picker, what it filters, and what it remembers."""
import csv
import os
from datetime import date

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox  # noqa: E402

from subsentiment.gui import days  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.reddit import Comment, Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

DAY = 86400
D1, D2, D3 = (days.from_date(date(2026, 10, d)) for d in (5, 6, 9))  # 7 and 8 Oct have nothing saved
SEP = days.from_date(date(2026, 9, 30))


def settings_in(tmp_path, name="days-test"):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    return QSettings()


def test_choice_text_names_the_days_briefly():
    have = {SEP, D1, D2, D3}
    assert days.choice_text(None, have) == "All days"
    assert days.choice_text(set(have), have) == "All days"
    assert days.choice_text(set(), have) == "No days"
    assert days.choice_text({D2}, have) == "6 Oct"
    assert days.choice_text({D1, D2}, have) == "5 – 6 Oct"
    assert days.choice_text({SEP, D1 - DAY * 4}, {SEP, D1 - DAY * 4}) == "All days"
    assert days.choice_text({SEP, D1 - 4 * DAY}, have | {D1 - 4 * DAY}) == "30 Sep – 1 Oct"
    assert days.choice_text({D1, D3}, have) == "2 of 4 days"  # not a run: 7 and 8 Oct are left out


def test_a_choice_is_remembered_per_subreddit_and_every_day_is_stored_as_none(tmp_path):
    s = settings_in(tmp_path)
    try:
        have = {D1, D2, D3}
        assert days.load_choice(s, "Python") is None
        assert days.save_choice(s, "Python", {D1, D3}, have) == {D1, D3}
        assert days.load_choice(s, "python") == {D1, D3}  # names are case-insensitive, like the store's
        assert days.load_choice(s, "rust") is None
        assert days.save_choice(s, "Python", set(), have) == set()
        assert days.load_choice(s, "Python") == set()  # nothing chosen is a choice too
        assert days.save_choice(s, "Python", {D1, D2, D3}, have) is None  # every day: days fetched later count
        assert not s.contains(days.settings_key("Python"))
    finally:
        QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def test_keep_filters_rows_by_their_own_utc_day():
    rows = [{"created_utc": D1 + 10}, {"created_utc": D2 + DAY - 1}, {"created_utc": D3}]
    assert days.keep(rows, None) == rows
    assert days.keep(rows, {D2, D3}) == rows[1:]
    assert days.keep(rows, set()) == []


def test_calendar_grid_starts_on_the_locales_first_weekday(monkeypatch):
    QApplication.instance() or QApplication([])
    monkeypatch.setattr(days, "first_weekday", lambda: 0)  # Monday
    cal = days.DayCalendar()
    cal.show_month(2026, 10)  # 1 Oct 2026 is a Thursday
    slots = cal.days_shown()
    assert slots[:3] == [None] * 3 and slots[3] == days.from_date(date(2026, 10, 1))
    assert cal.rows() == 5
    monkeypatch.setattr(days, "first_weekday", lambda: 6)  # Sunday
    cal.show_month(2026, 2)  # 1 Feb 2026 is a Sunday and February has exactly four weeks
    assert cal.days_shown()[0] == days.from_date(date(2026, 2, 1)) and cal.rows() == 4


# -- in the main window -----------------------------------------------------------------------------


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
        w._refresh_datasets(select="Python")
        windows.append(w)
        return w
    yield make
    for w in windows:
        w.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def click(w, day, modifier=Qt.KeyboardModifier.NoModifier):
    cal = w.days_pick.picker.calendar
    QTest.mouseClick(cal, Qt.MouseButton.LeftButton, modifier, cal.rect_of(day).center())


def test_every_view_counts_only_the_chosen_days(window, tmp_path, monkeypatch):
    w = window()
    assert w.days_pick.currentText() == "All days" and w.overview.tiles["count"].value.text() == "10"
    assert w.days_pick.isEnabled()
    w.days_pick.showPopup()
    cal = w.days_pick.picker.calendar
    assert (cal.year, cal.month) == (2026, 10) and set(cal.counts) == {D1, D2, D3}
    click(w, D2)  # drop 6 Oct
    click(w, D1 + 2 * DAY)  # 7 Oct has nothing saved: not choosable
    w.days_pick.hidePopup()  # applies at once rather than after the settling delay
    assert w.chosen_days == {D1, D3}
    assert w.days_pick.currentText() == "2 of 3 days"
    assert w.overview.tiles["count"].value.text() == "8" and len(w.posts) == 4 and len(w.comments) == 4
    assert w.posts_tab.model.rowCount() == 4 and w.comments_tab.model.rowCount() == 4
    assert w.tabbar.tabText(1) == "Posts  4" and w.tabbar.tabText(2) == "Comments  4"
    assert w.subheading.full_text().startswith("Last fetched ")  # the counts are on the tabs, checked above
    shown = []
    monkeypatch.setattr(w.overview.timeline, "set_points", shown.append)
    w._render_overview()
    assert [d for d, _, _ in shown[-1]] == [D1, D3]  # the timeline too

    out = tmp_path / "posts.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    w._export("posts")
    assert sorted(r["id"] for r in csv.DictReader(open(out, encoding="utf-8-sig"))) == ["p0", "p1", "p3", "p4"]
    assert "from 2 chosen days" in w.statusBar().currentMessage()

    w._refresh_datasets(select="rust")  # another subreddit has its own choice: every day
    assert w.chosen_days is None and w.days_pick.currentText() == "All days" and len(w.posts) == 1
    w._refresh_datasets(select="Python")
    assert w.chosen_days == {D1, D3} and len(w.posts) == 4


def test_the_choice_survives_a_restart_and_all_days_resets_it(window):
    w = window()
    w.days_pick.showPopup()
    w.days_pick.picker.none_btn.click()
    click(w, D3)
    w.days_pick.hidePopup()
    assert w.days_pick.currentText() == "9 Oct" and w.overview.tiles["count"].value.text() == "4"
    w.close()

    again = window()
    assert again.chosen_days == {D3} and again.days_pick.currentText() == "9 Oct" and len(again.posts) == 2
    again.days_pick.showPopup()
    again.days_pick.picker.all_btn.click()
    again.days_pick.hidePopup()
    assert again.chosen_days is None and len(again.posts) == 5
    assert not again.settings.contains(days.settings_key("Python"))


def test_shift_click_and_drag_choose_runs_of_days(window):
    w = window()
    w.days_pick.showPopup()
    w.days_pick.picker.none_btn.click()
    click(w, D1)
    click(w, D3, modifier=Qt.KeyboardModifier.ShiftModifier)  # 5 to 9 Oct: the saved days among them
    assert w.days_pick.picker.chosen() == {D1, D2, D3}
    w.days_pick.picker.none_btn.click()
    cal = w.days_pick.picker.calendar
    QTest.mousePress(cal, Qt.MouseButton.LeftButton, pos=cal.rect_of(D1).center())
    QTest.mouseMove(cal, cal.rect_of(D2).center())
    QTest.mouseRelease(cal, Qt.MouseButton.LeftButton, pos=cal.rect_of(D2).center())
    assert w.days_pick.picker.chosen() == {D1, D2}
    # The keyboard works too: arrows move from the last day clicked, Space toggles.
    cal.setFocus()
    QTest.keyClick(cal, Qt.Key.Key_Right)
    QTest.keyClick(cal, Qt.Key.Key_Space)
    assert w.days_pick.picker.chosen() == {D1}
    QTest.keyClick(cal, Qt.Key.Key_Right)
    QTest.keyClick(cal, Qt.Key.Key_Space)  # 7 Oct has nothing saved
    assert w.days_pick.picker.chosen() == {D1}
    QTest.keyClick(cal, Qt.Key.Key_Up)
    QTest.keyClick(cal, Qt.Key.Key_Space)  # a week back is 30 Sep, outside this month: stays on 1 Oct
    QTest.keyClick(cal, Qt.Key.Key_Down)  # 8 Oct
    QTest.keyClick(cal, Qt.Key.Key_Right)  # 9 Oct
    QTest.keyClick(cal, Qt.Key.Key_Return)
    assert w.days_pick.picker.chosen() == {D1, D3}
    w.days_pick.hidePopup()
    assert w.chosen_days == {D1, D3}


def test_quick_choices_and_month_arrows(window):
    w = window()
    s = Store(w.db_path)
    s.save_post(Post("old", "Python", "september", "", "a", 1, 0.9, 0, SEP + 60, "https://r/old", ""), 0.1, "neutral")
    s.commit()
    s.close()
    w._refresh_datasets(select="Python")
    w.days_pick.showPopup()
    p = w.days_pick.picker
    assert p.title.text() == "October 2026" and p.prev.isEnabled() and not p.next.isEnabled()
    p.prev.click()
    assert p.title.text() == "September 2026" and not p.prev.isEnabled()
    p.month_btn.click()
    assert p.chosen() == {SEP}
    p.week_btn.click()
    assert p.chosen() == {D1, D2, D3}  # the 7 days up to the latest saved one
    assert "3 of 4 days" in p.summary.text()
    w.days_pick.hidePopup()
    assert w.days_pick.currentText() == "3 of 4 days"
    assert len(w.posts) == 5


def test_deleting_a_subreddit_forgets_its_days(window, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    w = window()
    w._on_days_chosen({D1})
    assert w.settings.contains(days.settings_key("Python"))
    w._delete_dataset()
    assert not w.settings.contains(days.settings_key("Python"))
    w._refresh_datasets(select="rust")
    w._on_days_chosen(set())
    w._delete_all()
    assert not w.settings.childGroups() or days.SETTINGS_GROUP not in w.settings.childGroups()
    assert not w.days_pick.isEnabled()  # nothing saved: nothing to pick


def test_a_refresh_while_the_calendar_is_open_keeps_the_users_pick(window):
    w = window()
    w.days_pick.showPopup()
    click(w, D2)  # still settling when a running fetch's once-a-second refresh arrives
    w._refresh_days()
    assert w.days_pick.picker.chosen() == {D1, D3} and w.days_pick.currentText() == "2 of 3 days"
    w.days_pick.hidePopup()
    assert w.chosen_days == {D1, D3}


def test_rows_without_a_date_are_left_out_of_a_narrower_choice():
    assert days.keep([{"created_utc": None}, {"created_utc": D1}], {D1}) == [{"created_utc": D1}]
    assert days.choice_text({days.from_date(date(2025, 12, 31)), days.from_date(date(2026, 1, 1))},
                            {days.from_date(date(2025, 12, 31)), days.from_date(date(2026, 1, 1)), D1}) == \
        "31 Dec 2025 – 1 Jan 2026"
