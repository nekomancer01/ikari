"""The window stays quick with a big saved subreddit: tables sort and filter in Python rather than through a
Python call per comparison, the Overview's totals are kept up as rows arrive, and the end of a fetch reads
back only what that fetch saved. Work is counted (calls into the model and the store), not timed, so these
hold on slow machines too."""
import os
import random
import sqlite3
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QItemSelectionModel, QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QTableView  # noqa: E402

from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import tables  # noqa: E402
from subsentiment.gui.tables import POST_COLUMNS, RowsModel  # noqa: E402
from subsentiment.analysis import Analysis  # noqa: E402
from subsentiment.store import Store  # noqa: E402

POSTS, COMMENTS = 300, 20_000
LABELS = ("positive", "neutral", "negative")


def fill(path, sub="Big"):
    """POSTS posts and COMMENTS comments for ``sub``, written straight to the database."""
    Store(path).close()
    rng = random.Random(7)
    now = time.time()
    db = sqlite3.connect(path)
    posts = [(f"p{i}", sub, f"title {i}", "", f"author{i % 40}", rng.randint(0, 9000), 0.9, rng.randint(0, 50),
              now - rng.uniform(0, 30 * 86400), f"/r/{sub}/comments/p{i}/", "", rng.uniform(-1, 1),
              LABELS[i % 3], now - 3600) for i in range(POSTS)]
    db.executemany(f"INSERT INTO posts VALUES ({','.join('?' * 14)})", posts)
    comments = [(f"c{j}", f"p{j % POSTS}", sub, f"t3_p{j % POSTS}", f"user{j % 500}",
                 rng.choice(["Love it", "awful", "fine I guess", "Great work"]) + f" {j}", rng.randint(-5, 900),
                 now - rng.uniform(0, 30 * 86400), 0, f"/r/{sub}/comments/c{j}/", rng.uniform(-1, 1), LABELS[j % 3],
                 now - 3600) for j in range(COMMENTS)]
    db.executemany(f"INSERT INTO comments VALUES ({','.join('?' * 13)})", comments)
    db.commit()
    db.close()


@pytest.fixture
def window(tmp_path):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("subsentiment-test")
    app.setApplicationName(tmp_path.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    QSettings().setValue("updates/auto", False)
    db = str(tmp_path / "big.db")
    fill(db)
    w = mw.MainWindow(db)
    yield w
    w.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def count_calls(monkeypatch, cls, name) -> list:
    calls = []
    real = getattr(cls, name)

    def counted(self, *a, **k):
        calls.append(1)
        return real(self, *a, **k)
    monkeypatch.setattr(cls, name, counted)
    return calls


def test_opening_a_big_subreddit_doesnt_ask_the_model_for_every_value_again_and_again(window, monkeypatch):
    calls = []

    class Counted(RowsModel):  # Qt calls an override it found when the model was made, so count in one
        def data(self, index, role=Qt.ItemDataRole.DisplayRole):
            calls.append(1)
            return super().data(index, role)
    monkeypatch.setattr(tables, "RowsModel", Counted)  # where TablePage makes its model
    w = mw.MainWindow(window.db_path)
    try:
        calls.clear()
        w._show_dataset("")
        w._show_dataset("Big")
        # Sorting 20,000 comments through a QSortFilterProxyModel asked for about 600,000 values, one Python
        # call each (17 s for 100,000 comments). Sorting in Python and sizing columns from their widest
        # values asks for a few hundred.
        assert w.comments_tab.model.rowCount() == COMMENTS
        assert len(calls) < 5_000
    finally:
        w.close()


def test_the_comments_table_opens_sorted_by_sentiment_and_sorts_by_any_column(window):
    tab = window.comments_tab
    shown = [tab.model.row_at(i)["sentiment"] for i in range(tab.model.rowCount())]
    assert shown == sorted(shown)
    tab.view.sortByColumn(3, Qt.SortOrder.DescendingOrder)  # Score
    scores = [tab.model.row_at(i)["score"] for i in range(tab.model.rowCount())]
    assert scores == sorted(scores, reverse=True)


def test_sorting_keeps_the_selected_row_selected():
    m = RowsModel(POST_COLUMNS)
    view = QTableView()
    view.setModel(m)
    view.setSortingEnabled(True)
    row = lambda i, s: {"id": f"p{i}", "sentiment": s, "label": "neutral", "title": f"t{i}", "selftext": "",  # noqa: E731
                        "score": 10 - i, "num_comments": None, "author": "a", "created_utc": 0.0}
    m.set_rows([row(1, 0.3), row(2, -0.5), row(3, 0.9)])
    view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
    assert [m.row_at(i)["id"] for i in range(3)] == ["p2", "p1", "p3"]
    view.selectionModel().select(m.index(2, 0), QItemSelectionModel.SelectionFlag.Select
                                 | QItemSelectionModel.SelectionFlag.Rows)
    view.sortByColumn(3, Qt.SortOrder.AscendingOrder)  # by score: p3 (7), p2 (8), p1 (9)
    assert [m.row_at(i)["id"] for i in range(3)] == ["p3", "p2", "p1"]
    assert [m.row_at(i.row())["id"] for i in view.selectionModel().selectedRows()] == ["p3"]


def test_rows_a_fetch_adds_go_where_the_sort_order_puts_them():
    m = RowsModel(POST_COLUMNS)
    row = lambda i, s: {"id": f"p{i}", "sentiment": s, "label": "neutral", "title": "t", "selftext": "",  # noqa: E731
                        "score": None, "num_comments": None, "author": "a", "created_utc": 0.0}
    m.set_rows([row(1, -0.5), row(2, 0.5)])
    m.sort(0, Qt.SortOrder.AscendingOrder)
    inserted = []
    m.rowsInserted.connect(lambda parent, a, b: inserted.append(a))
    assert m.append_rows([row(3, 0.0), row(4, 0.9), row(5, -0.9)]) == 3
    assert [m.row_at(i)["id"] for i in range(5)] == ["p5", "p1", "p3", "p2", "p4"]
    assert len(inserted) == 3  # each slotted in, not the whole table reset
    m.sort(0, Qt.SortOrder.DescendingOrder)
    m.append_rows([row(6, 0.1)])
    assert [m.row_at(i)["id"] for i in range(6)] == ["p4", "p2", "p6", "p3", "p1", "p5"]


def test_the_text_filter_ignores_case_and_line_breaks_and_applies_to_rows_a_fetch_adds(window):
    tab = window.comments_tab
    tab.filter.setText("LOVE  it")
    loved = sum(1 for r in tab.all_rows if r["body"].startswith("Love it"))
    assert 0 < tab.model.rowCount() == loved < COMMENTS
    assert all("love it" in tab.model.row_at(i)["body"].lower() for i in range(tab.model.rowCount()))
    tab.label_filter.setCurrentText("Negative")
    both = sum(1 for r in tab.all_rows if r["body"].startswith("Love it") and r["label"] == "negative")
    assert tab.model.rowCount() == both
    new = {"id": "new1", "post_id": "p1", "subreddit": "Big", "body": "I\nlove\tIT", "label": "negative",
           "sentiment": -0.2, "score": 1, "post_title": "t", "author": "x", "created_utc": 0.0, "fetched_at": 0.0}
    other = dict(new, id="new2", body="no")
    assert tab.add_rows([new, other]) == 2
    assert tab.model.rowCount() == both + 1 and tab.count.text() == mw.plural(both + 1, "row")
    tab.filter.setText("")
    tab.label_filter.setCurrentText("All labels")
    assert tab.model.rowCount() == COMMENTS + 2


def test_the_overview_counts_rows_as_they_arrive():
    rng = random.Random(3)
    rows = [{"id": str(i), "label": LABELS[i % 3], "sentiment": round(rng.uniform(-1, 1), 1),  # with ties
             "created_utc": i * 5000.0} for i in range(500)]
    t = Analysis(rows[:200])
    t.add(rows[200:450])
    t.add(rows[450:])
    whole = Analysis(rows)
    assert t.counts == whole.counts and t.count == 500 and abs(t.mean - sum(r["sentiment"] for r in rows) / 500) < 1e-9
    days = {}
    for r in rows:
        days.setdefault(int(r["created_utc"] // 86400 * 86400), []).append(r["sentiment"])
    assert [(d, round(m, 9), n) for d, m, n in t.daily_means] == \
           [(d, round(sum(v) / len(v), 9), len(v)) for d, v in sorted(days.items())]
    ranked = sorted(rows, key=lambda r: r["sentiment"])  # what the overview listed before it counted as rows came
    assert t.most_positive() == ranked[::-1][:10] and t.most_negative() == ranked[:10]
    both = Analysis.combined(Analysis(rows[:100]), Analysis(rows[100:]))
    assert both.counts == t.counts and abs(both.mean - t.mean) < 1e-9 and both.daily_means == whole.daily_means
    assert both.most_positive() == t.most_positive() and both.most_negative() == t.most_negative()


def test_the_end_of_a_fetch_reads_back_only_what_it_saved(window, monkeypatch):
    window._since = time.time() - 1
    writer = Store(window.db_path)
    db = writer.db
    db.execute("UPDATE posts SET score = 12345, fetched_at = ? WHERE id = 'p5'", (time.time(),))  # a new score
    db.execute("INSERT INTO comments SELECT 'fresh', post_id, subreddit, parent_id, author, 'a fresh comment', 1, "
               "created_utc, depth, permalink, 0.9, 'positive', ? FROM comments WHERE id = 'c1'", (time.time(),))
    writer.commit()
    writer.close()
    reads = count_calls(monkeypatch, Store, "comments") + count_calls(monkeypatch, Store, "posts")
    before = int(window.overview.tiles["count"].value.text().replace(",", ""))
    window._merge_fetched()
    assert reads == []  # not all 20,300 rows again
    posts, comments = window.posts_tab, window.comments_tab
    assert [posts.model.row_at(i)["score"] for i in range(POSTS) if posts.model.row_at(i)["id"] == "p5"] == [12345]
    assert comments.model.rowCount() == COMMENTS + 1 and len(window.comments) == COMMENTS + 1
    assert int(window.overview.tiles["count"].value.text().replace(",", "")) == before + 1
    assert window.tabbar.tabText(2) == f"Comments  {COMMENTS + 1:,}"


def test_switching_theme_recolours_without_reading_the_data_again(window, monkeypatch):
    reads = count_calls(monkeypatch, Store, "comments")
    window._set_theme("light")
    window._set_theme("dark")
    assert reads == [] and window.comments_tab.model.rowCount() == COMMENTS


def test_a_running_fetch_counts_its_rows_into_the_day_picker_without_recounting_every_day(window, monkeypatch):
    window.fetch_job.qthread = object()  # a fetch is "running" on the subreddit shown
    window.request = mw.collect.FetchRequest("Big")
    window._since = time.time() - 1
    window._read_to = {"posts": window._since, "comments": window._since}
    new_day = int(time.time() // 86400 * 86400) - 90 * 86400  # no saved rows that day yet
    writer = Store(window.db_path)
    writer.db.execute("UPDATE posts SET score = 1, fetched_at = ? WHERE id = 'p7'", (time.time(),))  # saved again
    writer.db.execute("INSERT INTO comments SELECT 'fresh', post_id, subreddit, parent_id, author, 'hi', 1, ?, depth, "
                      "permalink, 0.1, 'neutral', ? FROM comments WHERE id = 'c1'", (new_day + 60, time.time()))
    writer.commit()
    recounts = count_calls(monkeypatch, Store, "day_counts")
    try:
        window._queue_new_rows()
        window._live_refresh()
        assert recounts == []
        assert window._day_counts == window.store.day_counts("Big")
        assert window._day_counts[new_day] == (0, 1)
    finally:
        window.fetch_job.qthread = None
        writer.close()


def test_typing_a_filter_keeps_the_selected_row_selected(window):
    tab = window.comments_tab
    pick = next(i for i in range(tab.model.rowCount()) if tab.model.row_at(i)["body"].startswith("Great work"))
    picked = tab.model.row_at(pick)["id"]
    tab.view.selectRow(pick)
    tab.filter.setText("great")
    assert [tab.model.row_at(i.row())["id"] for i in tab.view.selectionModel().selectedRows()] == [picked]
    assert tab.model.row_at(tab.view.currentIndex().row())["id"] == picked
    tab.filter.setText("awful")  # it doesn't match: nothing is selected
    assert tab.view.selectionModel().selectedRows() == []


def test_rows_without_a_date_sort_with_the_dated_ones():
    m = RowsModel(POST_COLUMNS)
    row = lambda i, t: {"id": f"p{i}", "sentiment": 0.0, "label": "neutral", "title": "t", "selftext": "",  # noqa: E731
                        "score": None, "num_comments": None, "author": None, "created_utc": t}
    m.set_rows([row(1, 50.0), row(2, None), row(3, 10.0)])
    m.sort(6, Qt.SortOrder.AscendingOrder)  # Posted
    assert [m.row_at(i)["id"] for i in range(3)] == ["p2", "p3", "p1"]
    m.sort(5, Qt.SortOrder.AscendingOrder)  # Author, all unknown
    assert m.append_rows([row(4, None)]) == 1 and m.rowCount() == 4
