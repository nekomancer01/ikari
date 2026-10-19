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
    w.limit.setValue(250)
    w.include_comments.setChecked(False)
    w.comments_per_post.setValue(20)
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.tabbar.setCurrentIndex(1)
    w.close()

    again = make_window()
    assert again.subreddit.text() == "r/AskScience" and again.query.text() == "black holes"
    assert again.range.currentData() == "custom"
    assert (again.date_from.date(), again.date_to.date()) == (QDate(2026, 3, 1), QDate(2026, 3, 31))
    assert (again.limit.value(), again.include_comments.isChecked(), again.comments_per_post.value()) == (250, False, 20)
    assert not again.comments_per_post.isEnabled()
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
