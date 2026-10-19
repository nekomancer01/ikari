"""The fetch form and saved-data list survive a restart; the delete actions clear the store."""
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
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
    monkeypatch.setattr(mw.settings_dialog, "load", lambda s: {"client_id": "", "client_secret": ""})
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
    w.sort.setCurrentIndex(w.sort.findData("top"))
    w.time_filter.setCurrentIndex(w.time_filter.findData("year"))
    w.limit.setValue(250)
    w.include_comments.setChecked(True)
    w.comments_per_post.setValue(20)
    w.datasets.setCurrentRow([w.datasets.item(i).text() for i in range(w.datasets.count())].index("r/Python"))
    w.tabbar.setCurrentIndex(1)
    w.close()

    again = make_window()
    assert again.subreddit.text() == "r/AskScience" and again.query.text() == "black holes"
    assert again.sort.currentData() == "top" and again.time_filter.currentData() == "year"
    assert (again.limit.value(), again.include_comments.isChecked(), again.comments_per_post.value()) == (250, True, 20)
    assert again.comments_per_post.isEnabled()
    assert again.current == "Python" and again.tabbar.currentIndex() == 1


def test_sort_options_follow_search_and_time_range_only_where_it_applies(make_window):
    w = make_window()
    assert [w.sort.itemData(i) for i in range(w.sort.count())] == list(mw.LISTING_SORTS)
    w.sort.setCurrentIndex(w.sort.findData("rising"))
    assert not w.time_filter.isEnabled()
    w.query.setText("q")
    assert w.sort.currentData() == "relevance" and w.time_filter.isEnabled()
    w.query.setText("")
    assert w.sort.currentData() == "hot" and not w.time_filter.isEnabled()
    w.sort.setCurrentIndex(w.sort.findData("top"))
    assert w.time_filter.isEnabled()


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
