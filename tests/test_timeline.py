"""The Overview's timeline chart, driven in the real main window: a dated axis, the range buttons, zooming by
dragging, the wheel and the navigator strip, the hover box, and how it keeps up with the Days picker."""
import os
import time
from datetime import date, datetime, timezone

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QFontMetricsF, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from subsentiment.gui import charts, days, motion  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.reddit import Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

DAY = 86400
FIRST = days.from_date(date(2026, 7, 1))
N = 60  # 1 Jul to 29 Aug 2026, every day saved but 20 to 22 Jul
GAP = {FIRST + d * DAY for d in (19, 20, 21)}
SAVED = [FIRST + d * DAY for d in range(N) if FIRST + d * DAY not in GAP]
LAST_END = SAVED[-1] + DAY


def pump(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture
def window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    QSettings().setValue("ui/reduce_motion", True)  # the window sets motion from this when it opens
    s = Store(tmp_path / "t.db")
    for i, day in enumerate(SAVED):
        for k in range(1 + i % 3):
            score = ((i * 7 + k) % 11 - 5) / 10
            label = "positive" if score > 0.05 else "negative" if score < -0.05 else "neutral"
            s.save_post(Post(f"p{i}-{k}", "Python", f"t{i}", "", "a", 1, 0.9, 0, day + 3600 * (k + 1),
                             f"https://r/{i}/{k}", ""), score, label)
    s.save_post(Post("r0", "rust", "crab", "", "a", 1, 0.9, 0, FIRST + 3600, "https://r/r0", ""), 0.5, "positive")
    s.commit()
    s.close()
    motion.set_enabled(False)  # each change lands at once; the motion test turns it back on
    windows = []

    def make():
        w = mw.MainWindow(str(tmp_path / "t.db"))
        motion.set_enabled(False)  # the window turns motion on from its saved setting
        w.resize(1280, 721)
        w.show()
        w._refresh_datasets(select="Python")
        windows.append(w)
        pump(50)
        return w
    yield make
    for w in windows:
        w.close()
    motion.set_enabled(True)
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def x_of(tl, t: float) -> int:
    plot, _ = tl._areas()
    lo, hi, _ = tl._screen
    return round(tl._x(t, lo, hi, plot))


def wheel(tl, pos: QPoint, dy: int = 0, dx: int = 0, mods=Qt.KeyboardModifier.NoModifier) -> bool:
    """Whether the chart took the wheel turn (rather than leaving it to the page to scroll)."""
    e = QWheelEvent(QPointF(pos), QPointF(tl.mapToGlobal(pos)), QPoint(), QPoint(dx, dy), Qt.MouseButton.NoButton,
                    mods, Qt.ScrollPhase.NoScrollPhase, False)
    e.setAccepted(False)
    tl.wheelEvent(e)
    return e.isAccepted()


def test_it_starts_on_every_day_and_the_range_buttons_show_the_latest_days(window):
    tl = window().overview.timeline
    assert [d for d, _, _ in tl.points] == SAVED
    assert tl.view is None and tl._screen[:2] == (FIRST, LAST_END) and tl.presets["All"].isChecked()
    # 60 days: a six-month or one-year span would be every day anyway.
    assert [k for k, b in tl.presets.items() if b.isEnabled()] == ["1w", "1m", "All"]
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    assert tl.view == (LAST_END - 7 * DAY, LAST_END) and tl.preset == "1w"
    assert [b for k, b in tl.presets.items() if b.isChecked()] == [tl.presets["1w"]]
    assert [d for d, _, _ in tl.visible()] == SAVED[-7:]
    QTest.mouseClick(tl.presets["All"], Qt.MouseButton.LeftButton)
    assert tl.view is None and tl.presets["All"].isChecked() and not tl.presets["1w"].isChecked()


def test_the_axis_is_dated_and_steps_from_days_to_weeks_to_months(window):
    tl = window().overview.timeline
    # Widths in date labels, so the steps don't depend on the platform's font (Windows draws it wider).
    label = QFontMetricsF(charts._ui_font(9)).horizontalAdvance("30 Sep") + 20
    narrow = tl._ticks(LAST_END - 7 * DAY, LAST_END, label * 8)
    assert [text for _, text in narrow] == ["23 Aug", "24 Aug", "25 Aug", "26 Aug", "27 Aug", "28 Aug", "29 Aug"]
    weekly = tl._ticks(FIRST, LAST_END, label * 10)  # room for 9 Mondays, not for every other day
    assert [text for _, text in weekly][:3] == ["6 Jul", "13 Jul", "20 Jul"]  # Mondays
    assert all(datetime.fromtimestamp(t, timezone.utc).weekday() == 0 for t, _ in weekly)
    year = tl._ticks(days.from_date(date(2025, 11, 1)), days.from_date(date(2026, 9, 1)), label * 12)
    assert "2026" in [text for _, text in year] and "Dec" in [text for _, text in year]


def test_days_packed_too_close_are_shown_a_week_or_a_month_a_point(window):
    tl = window().overview.timeline
    assert tl.groups(FIRST, LAST_END, 600)[0] == "day"  # 10 px a day
    unit, weeks = tl.groups(FIRST, LAST_END, 120)  # 2 px a day: weeks, from Monday 29 Jun
    assert unit == "week" and all(datetime.fromtimestamp(s, timezone.utc).weekday() == 0 for s, *_ in weeks)
    assert sum(n for *_, n in weeks) == sum(n for *_, n in tl.points)
    first = [p for p in tl.points if p[0] < weeks[0][1]]
    assert weeks[0][2] == pytest.approx(sum(m * n for _, m, n in first) / sum(n for *_, n in first))  # by item
    assert len(weeks) == 9  # 20 to 22 Jul missing doesn't empty their week
    unit, months = tl.groups(FIRST, LAST_END, 30)
    assert unit == "month" and [(s, e) for s, e, *_ in months] == [
        (days.from_date(date(2026, 7, 1)), days.from_date(date(2026, 8, 1))),
        (days.from_date(date(2026, 8, 1)), days.from_date(date(2026, 9, 1)))]


def test_dragging_across_the_plot_zooms_to_those_days_and_a_double_click_shows_every_day(window):
    tl = window().overview.timeline
    plot, _ = tl._areas()
    y = round(plot.center().y())
    a, b = x_of(tl, FIRST + 10 * DAY), x_of(tl, FIRST + 30 * DAY)
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(a, y))
    QTest.mouseMove(tl, QPoint((a + b) // 2, y))
    QTest.mouseMove(tl, QPoint(b, y))
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(b, y))
    lo, hi = tl.view
    assert abs(lo - (FIRST + 10 * DAY)) < DAY / 2 and abs(hi - (FIRST + 30 * DAY)) < DAY / 2
    assert tl.preset is None and not any(b.isChecked() for b in tl.presets.values())
    QTest.mouseClick(tl, Qt.MouseButton.LeftButton, pos=QPoint(a + 3, y))  # a click isn't a drag: no zoom
    assert tl.view == (lo, hi)
    QTest.mouseDClick(tl, Qt.MouseButton.LeftButton, pos=QPoint(a, y))
    assert tl.view is None and tl.presets["All"].isChecked()


def test_the_wheel_zooms_round_the_pointer_and_shift_wheel_moves_sideways(window):
    tl = window().overview.timeline
    plot, _ = tl._areas()
    at = FIRST + 40 * DAY
    pos = QPoint(x_of(tl, at), round(plot.center().y()))
    assert wheel(tl, pos, dy=120)
    lo, hi = tl.view
    assert hi - lo == pytest.approx(N * DAY * 0.8)
    assert (at - lo) / (hi - lo) == pytest.approx((at - FIRST) / (N * DAY), abs=0.005)  # the day under the pointer stays put
    wheel(tl, pos, dy=120, mods=Qt.KeyboardModifier.ShiftModifier)
    assert tl.view[1] - tl.view[0] == pytest.approx(hi - lo) and tl.view[0] < lo  # earlier days, same span
    for _ in range(40):
        wheel(tl, pos, dy=120)
    assert tl.view[1] - tl.view[0] == charts.MIN_SPAN  # never closer than three days
    for _ in range(40):
        wheel(tl, pos, dy=-120)
    assert tl.view is None and tl.presets["All"].isChecked()
    assert not wheel(tl, pos, dy=-120)  # every day shown: scrolling down scrolls the page, as it used to


def test_the_navigator_window_moves_and_its_edges_resize_the_view(window):
    tl = window().overview.timeline
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    _, nav = tl._areas()
    y = round(nav.center().y())

    def nav_x(t):
        return round(tl._x(t, FIRST, LAST_END, nav))
    start = nav_x(LAST_END - 3.5 * DAY)
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(start, y))
    QTest.mouseMove(tl, QPoint(nav_x(LAST_END - 23.5 * DAY), y))  # 20 days earlier
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(nav_x(LAST_END - 23.5 * DAY), y))
    lo, hi = tl.view
    assert hi - lo == 7 * DAY and abs(hi - (LAST_END - 20 * DAY)) < DAY / 2 and tl.preset is None
    left = nav_x(lo)
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(left, y))
    QTest.mouseMove(tl, QPoint(nav_x(lo - 10 * DAY), y))
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(nav_x(lo - 10 * DAY), y))
    assert tl.view[1] == hi and abs(tl.view[0] - (lo - 10 * DAY)) < DAY / 2  # only the left edge moved
    lo = tl.view[0]
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(nav_x(lo), y))
    QTest.mouseMove(tl, QPoint(0, y))  # past the first day: the left edge stops there, the right one stays
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(0, y))
    assert tl.view == (FIRST, hi)
    QTest.mousePress(tl, Qt.MouseButton.LeftButton, pos=QPoint(nav_x(FIRST + 2 * DAY), y))  # outside: jump there
    QTest.mouseRelease(tl, Qt.MouseButton.LeftButton, pos=QPoint(nav_x(FIRST + 2 * DAY), y))
    assert tl.view[0] == FIRST


def test_hovering_a_day_picks_the_nearest_one(window):
    tl = window().overview.timeline
    QTest.mouseClick(tl.presets["1m"], Qt.MouseButton.LeftButton)
    plot, _ = tl._areas()
    day = SAVED[-5]
    QTest.mouseMove(tl, QPoint(x_of(tl, day + DAY * 0.6), round(plot.center().y())))
    assert tl._hover[:2] == ("day", day)
    before = tl.grab().toImage()
    tl.leaveEvent(None)
    assert tl._hover is None and tl.grab().toImage() != before  # the crosshair and box were drawn, and go


def test_days_a_running_fetch_adds_keep_a_zoom_on_the_latest_days(window):
    w = window()
    tl = w.overview.timeline
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    later = [*tl.points, (LAST_END, 0.2, 3), (LAST_END + DAY, -0.1, 2)]
    tl.set_points(later)
    assert tl.view == (LAST_END + 2 * DAY - 7 * DAY, LAST_END + 2 * DAY) and tl.preset == "1w"
    QTest.mouseClick(tl.presets["1m"], Qt.MouseButton.LeftButton)
    wheel(tl, QPoint(x_of(tl, LAST_END - 20 * DAY), 40), dy=120, mods=Qt.KeyboardModifier.ShiftModifier)
    fixed = tl.view
    tl.set_points([*later, (LAST_END + 2 * DAY, 0.3, 1)])
    assert tl.view == fixed  # looking at earlier days: they stay in view


def test_choosing_other_days_shows_every_day_again(window):
    w = window()
    tl = w.overview.timeline
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    w.days_pick.showPopup()
    w.days_pick.picker.none_btn.click()
    w.days_pick.picker._choose(set(SAVED[:30]))
    w.days_pick.hidePopup()
    assert [d for d, _, _ in tl.points] == SAVED[:30] and tl.view is None and tl.presets["All"].isChecked()
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    w._refresh_datasets(select="rust")
    assert tl.view is None and len(tl.points) == 1 and not tl.presets["1w"].isEnabled()


def test_zooming_eases_unless_motion_is_reduced(window, monkeypatch):
    monkeypatch.setattr(motion, "_os_allows", True)
    tl = window().overview.timeline
    motion.set_enabled(True)
    QTest.mouseClick(tl.presets["1w"], Qt.MouseButton.LeftButton)
    assert tl._zoom.running() and tl._screen[0] < LAST_END - 7 * DAY  # on its way, from every day
    pump(motion.DATA_MS + 200)
    assert tl._screen[:2] == (LAST_END - 7 * DAY, LAST_END)
    motion.set_enabled(False)  # View > Reduce motion
    QTest.mouseClick(tl.presets["All"], Qt.MouseButton.LeftButton)
    assert not tl._zoom.running() and tl._screen[:2] == (FIRST, LAST_END)
