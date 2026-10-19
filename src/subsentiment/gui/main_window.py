from __future__ import annotations

from PySide6.QtCore import QEvent, QSettings, QSortFilterProxyModel, Qt, QThread, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QSpinBox, QSplitter, QTableView,
    QTabWidget, QVBoxLayout, QWidget,
)

from subsentiment import collect
from subsentiment.gui import settings_dialog
from subsentiment.gui.charts import DistributionChart, TimelineChart
from subsentiment.gui.models import COMMENT_COLUMNS, POST_COLUMNS, SORT_ROLE, RowsModel
from subsentiment.gui.worker import FetchWorker
from subsentiment.reddit import LISTING_SORTS, SEARCH_SORTS, TIME_FILTERS
from subsentiment.store import Store, daily_means, summarize


class StatTile(QFrame):
    def __init__(self, caption: str):
        super().__init__()
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.value = QLabel("–")
        f = self.value.font()
        f.setPointSizeF(f.pointSizeF() * 1.8)
        f.setBold(True)
        self.value.setFont(f)
        cap = QLabel(caption)
        cap.setEnabled(False)  # secondary ink
        lay = QVBoxLayout(self)
        lay.addWidget(self.value)
        lay.addWidget(cap)

    def set(self, text: str) -> None:
        self.value.setText(text)


class TableTab(QWidget):
    """Filterable, sortable table; double-click opens the item on Reddit."""

    def __init__(self, columns, stretch_col: int):
        super().__init__()
        self.model = RowsModel(columns)
        self._all_rows_unfiltered: list = []
        self.proxy = QSortFilterProxyModel()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(stretch_col)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by text…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.label_filter = QComboBox()
        self.label_filter.addItems(["All labels", "Positive", "Neutral", "Negative"])
        self.label_filter.currentTextChanged.connect(self._apply_label_filter)
        self.count = QLabel()

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.verticalHeader().hide()
        self.view.setWordWrap(False)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
        self.view.doubleClicked.connect(self._open)

        top = QHBoxLayout()
        top.addWidget(self.filter, 1)
        top.addWidget(self.label_filter)
        top.addWidget(self.count)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.view)
        self.proxy.rowsInserted.connect(self._update_count)
        self.proxy.modelReset.connect(self._update_count)
        self.proxy.layoutChanged.connect(self._update_count)

    def _apply_label_filter(self, text: str) -> None:
        rows = self._all_rows_unfiltered
        if text != "All labels":
            rows = [r for r in rows if r["label"] == text.lower()]
        self.model.set_rows(rows)
        self._update_count()

    def load(self, rows) -> None:
        self._all_rows_unfiltered = rows
        self._apply_label_filter(self.label_filter.currentText())

    def _update_count(self, *args) -> None:
        self.count.setText(f"{self.proxy.rowCount()} rows")

    def _open(self, index) -> None:
        row = self.model.row_at(self.proxy.mapToSource(index).row())
        QDesktopServices.openUrl(QUrl(row["permalink"]))


class MainWindow(QMainWindow):
    def __init__(self, db_path: str):
        super().__init__()
        self.setWindowTitle("Subreddit Sentiment")
        self.resize(1280, 820)
        self.db_path = db_path
        self.store = Store(db_path)
        self.settings = QSettings()
        self.thread: QThread | None = None
        self.worker: FetchWorker | None = None
        self.current: str | None = None

        self._build_menu()
        splitter = QSplitter()
        splitter.addWidget(self._build_controls())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([320, 960])
        self.setCentralWidget(splitter)
        self.statusBar().showMessage(self._mode_text())
        self._refresh_datasets()

    # -- layout ---------------------------------------------------------------

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.export_posts = QAction("Export posts as CSV…", self, triggered=lambda: self._export("posts"))
        self.export_comments = QAction("Export comments as CSV…", self, triggered=lambda: self._export("comments"))
        file_menu.addAction(self.export_posts)
        file_menu.addAction(self.export_comments)
        file_menu.addSeparator()
        file_menu.addAction(QAction("Reddit API settings…", self, triggered=self._open_settings))
        file_menu.addSeparator()
        file_menu.addAction(QAction("Quit", self, shortcut=QKeySequence.StandardKey.Quit, triggered=self.close))

    def _build_controls(self) -> QWidget:
        fetch = QGroupBox("Fetch")
        self.subreddit = QLineEdit()
        self.subreddit.setPlaceholderText("e.g. python")
        self.subreddit.returnPressed.connect(self._start_fetch)
        self.query = QLineEdit()
        self.query.setPlaceholderText("optional: search within subreddit")
        self.query.textChanged.connect(self._update_sort_options)
        self.sort = QComboBox()
        self.sort.currentTextChanged.connect(self._update_time_enabled)
        self.time_filter = QComboBox()
        self.time_filter.addItems(TIME_FILTERS)
        self.time_filter.setCurrentText("week")
        self.limit = QSpinBox()
        self.limit.setRange(1, 1000)
        self.limit.setValue(100)
        self.include_comments = QCheckBox("Also fetch comments")
        self.comments_per_post = QSpinBox()
        self.comments_per_post.setRange(1, 500)
        self.comments_per_post.setValue(50)
        self.comments_per_post.setEnabled(False)
        self.include_comments.toggled.connect(self.comments_per_post.setEnabled)
        self._update_sort_options()

        form = QFormLayout()
        form.addRow("Subreddit  r/", self.subreddit)
        form.addRow("Search", self.query)
        form.addRow("Sort", self.sort)
        form.addRow("Time range", self.time_filter)
        form.addRow("Max posts", self.limit)
        form.addRow(self.include_comments)
        form.addRow("Comments / post", self.comments_per_post)

        self.fetch_btn = QPushButton("Fetch && analyse")
        self.fetch_btn.setDefault(True)
        self.fetch_btn.clicked.connect(self._start_fetch)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_fetch)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress_msg = QLabel()
        self.progress_msg.setWordWrap(True)
        buttons = QHBoxLayout()
        buttons.addWidget(self.fetch_btn, 1)
        buttons.addWidget(self.stop_btn)
        fl = QVBoxLayout(fetch)
        fl.addLayout(form)
        fl.addLayout(buttons)
        fl.addWidget(self.progress)
        fl.addWidget(self.progress_msg)

        data = QGroupBox("Saved data")
        self.datasets = QListWidget()
        self.datasets.currentTextChanged.connect(self._show_dataset)
        self.delete_btn = QPushButton("Delete selected")
        self.delete_btn.clicked.connect(self._delete_dataset)
        dl = QVBoxLayout(data)
        dl.addWidget(self.datasets)
        dl.addWidget(self.delete_btn)

        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.addWidget(fetch)
        lay.addWidget(data, 1)
        return panel

    def _build_tabs(self) -> QWidget:
        self.tabs = QTabWidget()

        overview = QWidget()
        self.scope = QComboBox()
        self.scope.addItems(["Posts and comments", "Posts only", "Comments only"])
        self.scope.currentTextChanged.connect(lambda _: self._render_overview())
        self.heading = QLabel("No data yet. Enter a subreddit and press Fetch.")
        f = self.heading.font()
        f.setPointSizeF(f.pointSizeF() * 1.3)
        f.setBold(True)
        self.heading.setFont(f)
        top = QHBoxLayout()
        top.addWidget(self.heading, 1)
        top.addWidget(QLabel("Include:"))
        top.addWidget(self.scope)

        self.tiles = {k: StatTile(c) for k, c in [
            ("count", "items analysed"), ("mean", "mean sentiment (−1 to +1)"),
            ("positive", "positive"), ("negative", "negative")]}
        tiles = QHBoxLayout()
        for t in self.tiles.values():
            tiles.addWidget(t)

        self.distribution = DistributionChart()
        self.timeline = TimelineChart()
        charts = QHBoxLayout()
        charts.addWidget(self.distribution, 2)
        charts.addWidget(self.timeline, 3)

        self.most_positive = QListWidget()
        self.most_negative = QListWidget()
        for lw in (self.most_positive, self.most_negative):
            lw.itemDoubleClicked.connect(lambda item: QDesktopServices.openUrl(QUrl(item.data(Qt.ItemDataRole.UserRole))))
        extremes = QGridLayout()
        extremes.addWidget(QLabel("<b>Most positive</b>"), 0, 0)
        extremes.addWidget(QLabel("<b>Most negative</b>"), 0, 1)
        extremes.addWidget(self.most_positive, 1, 0)
        extremes.addWidget(self.most_negative, 1, 1)

        ol = QVBoxLayout(overview)
        ol.addLayout(top)
        ol.addLayout(tiles)
        ol.addLayout(charts, 3)
        ol.addLayout(extremes, 2)

        self.posts_tab = TableTab(POST_COLUMNS, stretch_col=2)
        self.comments_tab = TableTab(COMMENT_COLUMNS, stretch_col=2)
        self.tabs.addTab(overview, "Overview")
        self.tabs.addTab(self.posts_tab, "Posts")
        self.tabs.addTab(self.comments_tab, "Comments")
        return self.tabs

    # -- control state ------------------------------------------------------

    def _update_sort_options(self) -> None:
        options = SEARCH_SORTS if self.query.text().strip() else LISTING_SORTS
        current = self.sort.currentText()
        self.sort.blockSignals(True)
        self.sort.clear()
        self.sort.addItems(options)
        if current in options:
            self.sort.setCurrentText(current)
        self.sort.blockSignals(False)
        self._update_time_enabled()

    def _update_time_enabled(self) -> None:
        searching = bool(self.query.text().strip())
        self.time_filter.setEnabled(searching or self.sort.currentText() in ("top", "controversial"))

    def _mode_text(self) -> str:
        creds = settings_dialog.load(self.settings)
        if creds["client_id"] and creds["client_secret"]:
            return "Using Reddit API credentials."
        return "Anonymous mode (slow). Add API credentials under File → Reddit API settings for faster, reliable fetching."

    def _set_busy(self, busy: bool) -> None:
        self.fetch_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.progress.setVisible(busy)
        for w in (self.subreddit, self.query, self.sort, self.limit, self.include_comments):
            w.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)
            self.time_filter.setEnabled(False)
            self.comments_per_post.setEnabled(False)
        else:
            self._update_time_enabled()
            self.comments_per_post.setEnabled(self.include_comments.isChecked())

    # -- fetching -------------------------------------------------------------

    def _start_fetch(self) -> None:
        if self.thread is not None:
            return
        name = self.subreddit.text().strip()
        if not name:
            self.subreddit.setFocus()
            return
        req = collect.FetchRequest(
            subreddit=name, sort=self.sort.currentText(), time_filter=self.time_filter.currentText(),
            limit=self.limit.value(), query=self.query.text().strip(),
            include_comments=self.include_comments.isChecked(),
            comments_per_post=self.comments_per_post.value())
        self.thread = QThread(self)
        self.worker = FetchWorker(settings_dialog.load(self.settings), req, self.db_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_done)
        self._set_busy(True)
        self.progress_msg.setText(f"Contacting Reddit for r/{name}…")
        self.thread.start()

    def _stop_fetch(self) -> None:
        if self.worker:
            self.worker.stop()
            self.progress_msg.setText("Stopping after the current request…")
            self.stop_btn.setEnabled(False)

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.progress_msg.setText(msg)

    def _on_finished(self, result: collect.FetchResult) -> None:
        if result.posts == 0:
            self.progress_msg.setText("No posts found.")
            return
        extra = f" and {result.comments} comments" if result.comments else ""
        stopped = " (stopped early)" if result.stopped else ""
        self.progress_msg.setText(f"Analysed {result.posts} posts{extra} from r/{result.subreddit}{stopped}.")
        self._refresh_datasets(select=result.subreddit)

    def _on_failed(self, message: str) -> None:
        self.progress_msg.setText("Fetch failed.")
        QMessageBox.warning(self, "Fetch failed", message)
        self._refresh_datasets(select=self.current)  # show anything stored before the error

    def _on_thread_done(self) -> None:
        self.thread.deleteLater()
        self.worker.deleteLater()
        self.thread = self.worker = None
        self._set_busy(False)

    # -- datasets -------------------------------------------------------------

    def _refresh_datasets(self, select: str | None = None) -> None:
        names = self.store.subreddits()
        select = select or self.current or (names[0] if names else None)
        self.datasets.blockSignals(True)
        self.datasets.clear()
        for n in names:
            self.datasets.addItem(n)
        self.datasets.blockSignals(False)
        match = next((i for i, n in enumerate(names) if select and n.lower() == select.lower()), None)
        if match is not None:
            self.datasets.setCurrentRow(match)  # emits currentTextChanged -> _show_dataset
        else:
            self._show_dataset("")

    def _show_dataset(self, name: str) -> None:
        self.current = name or None
        self.posts = self.store.posts(name) if name else []
        self.comments = self.store.comments(name) if name else []
        self.posts_tab.load(self.posts)
        self.comments_tab.load(self.comments)
        self.export_posts.setEnabled(bool(self.posts))
        self.export_comments.setEnabled(bool(self.comments))
        self.delete_btn.setEnabled(bool(name))
        self.tabs.setTabText(1, f"Posts ({len(self.posts)})")
        self.tabs.setTabText(2, f"Comments ({len(self.comments)})")
        self._render_overview()

    def _render_overview(self) -> None:
        if not self.current:
            self.heading.setText("No data yet. Enter a subreddit and press Fetch.")
            rows = []
        else:
            self.heading.setText(f"r/{self.current}")
            scope = self.scope.currentText()
            rows = (list(self.posts) if scope != "Comments only" else []) + \
                   (list(self.comments) if scope != "Posts only" else [])
        s = summarize(rows)
        n = s["count"]
        self.tiles["count"].set(f"{n:,}")
        self.tiles["mean"].set(f"{s['mean']:+.3f}" if n else "–")
        self.tiles["positive"].set(f"{100 * s['counts']['positive'] / n:.0f}%" if n else "–")
        self.tiles["negative"].set(f"{100 * s['counts']['negative'] / n:.0f}%" if n else "–")
        self.distribution.set_counts(s["counts"])
        self.timeline.set_points(daily_means(rows))

        ranked = sorted(rows, key=lambda r: r["sentiment"])
        self._fill_extremes(self.most_positive, list(reversed(ranked[-10:])))
        self._fill_extremes(self.most_negative, ranked[:10])

    @staticmethod
    def _fill_extremes(widget: QListWidget, rows) -> None:
        widget.clear()
        for r in rows:
            text = r["title"] if "title" in r.keys() else r["body"]
            text = " ".join((text or "").split())
            item = QListWidgetItem(f"{r['sentiment']:+.2f}   {text[:140]}")
            item.setToolTip(text[:1000])
            item.setData(Qt.ItemDataRole.UserRole, r["permalink"])
            widget.addItem(item)

    def _delete_dataset(self) -> None:
        if not self.current:
            return
        if QMessageBox.question(self, "Delete data", f"Delete all stored posts and comments for r/{self.current}?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_subreddit(self.current)
        self.current = None
        self._refresh_datasets()

    def _export(self, kind: str) -> None:
        if not self.current:
            return
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind}", f"{self.current}_{kind}.csv", "CSV files (*.csv)")
        if path:
            n = self.store.export_csv(self.current, kind, path)
            self.statusBar().showMessage(f"Exported {n} {kind} to {path}", 8000)

    def _open_settings(self) -> None:
        if settings_dialog.SettingsDialog(self.settings, self).exec():
            self.statusBar().showMessage(self._mode_text())

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.PaletteChange and hasattr(self, "posts"):
            self._show_dataset(self.current or "")  # re-pick chart and swatch colors for the new theme

    def closeEvent(self, event) -> None:
        if self.worker:
            self.worker.stop()
            self.thread.quit()
            self.thread.wait(3000)
        self.store.close()
        super().closeEvent(event)
