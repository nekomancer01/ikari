"""Fades: new data and view changes fade in, while every value is the new one at once and nothing moves.

Each test drives the real MainWindow offscreen and checks both halves: what's drawn is mid-fade for a moment,
and what the window reports (text, counts, bar shares) is already the final state. With Reduce motion on,
everything changes at once."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel  # noqa: E402

from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import motion  # noqa: E402
from subsentiment.reddit import Post  # noqa: E402
from subsentiment.store import Store  # noqa: E402

DAY = 86400
START = 1_700_000_000.0


def pump(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.005)


def snapshots(widget) -> list:
    return [c for c in widget.findChildren(QLabel, options=Qt.FindChildOption.FindDirectChildrenOnly)
            if c.objectName() == "motion-snapshot" and c.isVisible()]


def drawn(label: QLabel) -> str:
    """What a counting label paints now (its text() is the final value)."""
    return QLabel.text(label)


def save(store: Store, sub: str, n: int, label: str, first: int = 0, score: float | None = None) -> None:
    score = {"positive": 0.6, "negative": -0.6, "neutral": 0.0}[label] if score is None else score
    for i in range(first, first + n):
        store.save_post(Post(f"{sub}{label}{i}", sub, f"{label} title {i}", "", "a", 1, 0.9, 0,
                             START + (i % 5) * DAY, f"https://r/{sub}/{i}", ""), score, label)
    store.commit()


@pytest.fixture
def make_window(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    db = str(tmp_path / "m.db")
    s = Store(db)
    save(s, "Python", 30, "positive")
    save(s, "Python", 10, "negative")
    save(s, "rust", 5, "positive")
    save(s, "rust", 25, "negative")
    s.close()
    # The fades follow Windows' "Animation effects" too; a test machine with it off still checks them.
    monkeypatch.setattr(motion, "_os_allows", True)
    motion.set_enabled(True)
    windows = []

    def _make():
        w = mw.MainWindow(db)
        w.resize(1280, 721)
        w.show()
        windows.append(w)
        pump(100)
        return w
    yield _make
    for w in windows:
        w.close()
    motion.set_enabled(True)
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def pick(w, name: str) -> None:
    rows = [w.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(w.datasets.count())]
    w.datasets.setCurrentRow(rows.index(name))


def test_switching_subreddit_fades_and_counts_to_the_new_values(make_window):
    w = make_window()
    pick(w, "Python")
    pump(motion.DATA_MS + 300)
    assert w.overview.tiles["count"].value.text() == "40"
    chart = w.overview.distribution.chart()

    pick(w, "rust")
    # The new values are there at once...
    assert w.overview.tiles["count"].value.text() == "30" and w.overview.tiles["negative"].value.text() == "83%"
    assert w.overview.tiles["negative"].meter.share == pytest.approx(25 / 30)
    # ...the old view fades out over them, the cards fade in, and the numbers count down to them.
    assert snapshots(w.content)
    assert any(isinstance(c.graphicsEffect(), QGraphicsOpacityEffect) for c in w.overview.cards)
    pump(motion.DATA_MS // 3)
    assert drawn(w.overview.tiles["count"].value) not in ("40", "30")
    heights = w.overview.distribution.shown_heights()
    assert 10 < heights[0] < 25 and 5 < heights[2] < 30  # negative growing, positive shrinking
    assert w.overview.distribution.chart() is chart  # updated in place, not rebuilt

    pump(motion.DATA_MS + 6 * motion.STAGGER_MS + 300)
    assert drawn(w.overview.tiles["count"].value) == "30" and drawn(w.overview.tiles["negative"].value) == "83%"
    assert w.overview.distribution.shown_heights() == [25.0, 0.0, 5.0]
    assert not snapshots(w.content)
    assert all(c.graphicsEffect() is None for c in w.overview.cards)  # painted plainly again once faded in


def test_tabs_crossfade(make_window):
    w = make_window()
    w.tabbar.setCurrentIndex(0)
    pump(motion.VIEW_MS + 100)
    w.tabbar.setCurrentIndex(1)
    assert w.pages.currentIndex() == 1 and snapshots(w.content)
    pump(motion.VIEW_MS + 150)
    assert not snapshots(w.content)


def test_new_data_while_shown_counts_up_and_new_extremes_fade_in(make_window):
    w = make_window()
    pick(w, "Python")
    pump(motion.DATA_MS + 6 * motion.STAGGER_MS + 300)
    s = Store(w.db_path)
    save(s, "Python", 20, "negative", first=100, score=-0.9)  # the most negative yet
    s.close()
    shown = {r["id"] for r in w.posts_tab.all_rows}
    new = [r for r in w.store.posts("Python") if r["id"] not in shown]
    w._add_rows("posts", w.posts_tab, new)  # what a running fetch does with the rows it saves
    w._render_overview()
    assert w.overview.tiles["count"].value.text() == "60"
    assert snapshots(w.overview.most_negative.viewport())  # its entries changed
    assert not snapshots(w.overview.most_positive.viewport())  # its entries didn't: left alone
    pump(motion.DATA_MS // 3)
    assert 40 < int(drawn(w.overview.tiles["count"].value)) < 60
    pump(motion.DATA_MS + 200)
    assert drawn(w.overview.tiles["count"].value) == "60"


def test_a_snapshot_never_outlives_a_resize(make_window):
    w = make_window()
    w.tabbar.setCurrentIndex(1)
    assert snapshots(w.content)
    w.resize(1100, 700)
    pump(50)
    assert not snapshots(w.content)


def test_reduce_motion_changes_everything_at_once(make_window):
    w = make_window()
    assert not w.motion_act.isChecked()
    w.motion_act.setChecked(True)
    assert QSettings().value("ui/reduce_motion", False, bool) and not motion.enabled()
    pick(w, "Python")
    pick(w, "rust")
    assert not snapshots(w.content)
    assert all(c.graphicsEffect() is None for c in w.overview.cards)
    assert drawn(w.overview.tiles["count"].value) == "30" and w.overview.distribution.shown_heights() == [25.0, 0.0, 5.0]
    w.tabbar.setCurrentIndex(2)
    assert not snapshots(w.content)
    w.close()

    motion.set_enabled(True)
    again = make_window()  # the choice is kept and applies from the start
    assert again.motion_act.isChecked() and not motion.enabled()


def test_a_status_line_fades_in_when_it_appears(make_window):
    w = make_window()
    w.subreddit.setText("not a sub!!")
    w._start_fetch()  # refused before anything is requested
    assert "valid subreddit" in w.progress_msg.text()
    assert isinstance(w.progress_msg.graphicsEffect(), QGraphicsOpacityEffect)
    pump(motion.APPEAR_MS + 150)
    assert w.progress_msg.graphicsEffect() is None


def test_the_status_bar_fades_in_once_not_on_every_message(make_window):
    w = make_window()
    bar = w.statusBar()
    bar.showMessage("Exported 40 posts")
    assert isinstance(bar.graphicsEffect(), QGraphicsOpacityEffect)
    pump(motion.APPEAR_MS + 150)
    bar.showMessage("Deleted r/rust")  # already showing: no flash back to transparent
    assert bar.graphicsEffect() is None


def test_behind_a_native_backdrop_the_new_look_fades_in_instead(make_window):
    w = make_window()
    w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)  # what Mica or Acrylic sets up
    w.tabbar.setCurrentIndex(1)
    assert not snapshots(w.content)  # no still of see-through glass laid over the glass
    assert isinstance(w.content.graphicsEffect(), QGraphicsOpacityEffect)
    pump(motion.VIEW_MS + 150)
    assert w.content.graphicsEffect() is None


def test_qt_effects_the_os_has_off_stay_off(make_window, monkeypatch):
    effects = (Qt.UIEffect.UI_General, *motion._EFFECTS)
    before = {e: QApplication.isEffectEnabled(e) for e in effects}
    try:
        monkeypatch.setattr(motion, "_os_effects", None)
        QApplication.setEffectEnabled(Qt.UIEffect.UI_General, True)
        QApplication.setEffectEnabled(Qt.UIEffect.UI_FadeMenu, False)  # "Fade or slide menus" off
        QApplication.setEffectEnabled(Qt.UIEffect.UI_AnimateCombo, True)
        motion.set_enabled(True)
        assert not QApplication.isEffectEnabled(Qt.UIEffect.UI_FadeMenu)
        assert QApplication.isEffectEnabled(Qt.UIEffect.UI_AnimateCombo)
        motion.set_enabled(False)
        assert not QApplication.isEffectEnabled(Qt.UIEffect.UI_AnimateCombo)
    finally:
        for e, on in before.items():
            QApplication.setEffectEnabled(e, on)
