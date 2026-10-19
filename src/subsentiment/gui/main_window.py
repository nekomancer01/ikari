from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QEvent, QRectF, QSettings, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QGuiApplication, QIcon, QKeySequence, QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QSpinBox, QStackedWidget, QTableView, QTabBar,
    QVBoxLayout, QWidget,
)

from subsentiment import __version__, collect
from subsentiment.gui import native, settings_dialog, theme
from subsentiment.gui.charts import DistributionChart, TimelineChart, palette
from subsentiment.gui.models import COMMENT_COLUMNS, POST_COLUMNS, SORT_ROLE, RowsModel
from subsentiment.gui.theme import mark, paint_backdrop
from subsentiment.gui.update_flow import UpdateFlow
from subsentiment.gui.worker import FetchWorker
from subsentiment.reddit import LISTING_SORTS, SEARCH_SORTS, TIME_FILTERS
from subsentiment.store import Store, daily_means, summarize


def panel(kind: str = "true", margins: int = 18, spacing: int = 12) -> tuple[QFrame, QVBoxLayout]:
    """A rounded panel: kind "true" is glass, "tile" a brighter glass card, "solid" a near-opaque data surface."""
    frame = mark(QFrame(), "glass", kind)
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(margins, margins, margins, margins)
    lay.setSpacing(spacing)
    return frame, lay


def label(text: str, role: str) -> QLabel:
    return mark(QLabel(text), "role", role)


def section(text: str) -> QLabel:
    return label(text.upper(), "section")


class Meter(QWidget):
    """Thin rounded bar showing a share from 0 to 1."""

    def __init__(self, color_key: str):
        super().__init__()
        self.color_key = color_key
        self.share = 0.0
        self.setFixedHeight(6)

    def set_share(self, share: float) -> None:
        self.share = max(0.0, min(1.0, share))
        self.update()

    def _show_about(self) -> None:
        QMessageBox.about(self, "About ikari", (
            f"<h3>ikari {__version__}</h3>"
            "<p>Gather subreddit posts and comments and score their sentiment with VADER.</p>"
            "<p>Fonts: Manrope and Sora, both under the SIL Open Font License 1.1.</p>"))

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        r = QRectF(self.rect())
        track = QColor(*theme.tokens()["edge"][:3])
        track.setAlpha(40)
        p.setBrush(track)
        p.drawRoundedRect(r, 3, 3)
        if self.share > 0:
            p.setBrush(QColor(palette()[self.color_key]))
            p.drawRoundedRect(QRectF(r.x(), r.y(), max(r.height(), r.width() * self.share), r.height()), 3, 3)


class StatTile(QFrame):
    def __init__(self, caption: str, meter: str | None = None):
        super().__init__()
        mark(self, "glass", "tile")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 14, 18, 14)
        lay.setSpacing(4)
        self.value = label("–", "value")
        lay.addWidget(label(caption.upper(), "section"))
        lay.addWidget(self.value)
        self.meter = Meter(meter) if meter else None
        if self.meter:
            lay.addSpacing(4)
            lay.addWidget(self.meter)
        else:
            lay.addSpacing(10)

    def set(self, text: str, share: float | None = None) -> None:
        self.value.setText(text)
        if self.meter is not None:
            self.meter.set_share(share or 0.0)


class TablePage(QWidget):
    """Filterable, sortable table on a solid panel; double-click opens the item on Reddit."""

    def __init__(self, columns, stretch_col: int):
        super().__init__()
        self.model = RowsModel(columns)
        self._all_rows: list = []
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
        self.count = label("", "muted")

        self.view = QTableView()
        self.view.setModel(self.proxy)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        self.view.setMouseTracking(True)
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(34)
        self.view.setWordWrap(False)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.view.doubleClicked.connect(self._open)

        card, inner = panel("solid", margins=14, spacing=10)
        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.filter, 1)
        top.addWidget(self.label_filter)
        top.addWidget(self.count)
        inner.addLayout(top)
        inner.addWidget(self.view)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(card)
        for sig in (self.proxy.rowsInserted, self.proxy.rowsRemoved, self.proxy.modelReset, self.proxy.layoutChanged):
            sig.connect(self._update_count)

    def load(self, rows) -> None:
        self._all_rows = rows
        self._apply_label_filter(self.label_filter.currentText())

    def _apply_label_filter(self, text: str) -> None:
        rows = self._all_rows
        if text != "All labels":
            rows = [r for r in rows if r["label"] == text.lower()]
        self.model.set_rows(rows)
        self._update_count()

    def _update_count(self, *args) -> None:
        self.count.setText(f"{self.proxy.rowCount():,} rows")

    def _open(self, index) -> None:
        row = self.model.row_at(self.proxy.mapToSource(index).row())
        QDesktopServices.openUrl(QUrl(row["permalink"]))


class MainWindow(QMainWindow):
    def __init__(self, db_path: str):
        super().__init__()
        self.setWindowTitle("ikari")
        self.resize(1320, 860)
        self.db_path = db_path
        self.store = Store(db_path)
        self.settings = QSettings()
        self.updates = UpdateFlow(self, self.settings)
        self.thread: QThread | None = None
        self.worker: FetchWorker | None = None
        self.current: str | None = None
        self.posts: list = []
        self.comments: list = []
        self.setWindowIcon(QIcon(str(theme.ASSETS / "app.png")))
        # A native material (Mica/Acrylic/vibrancy) needs a translucent window from the start,
        # so a change applies on next launch.
        self._native_backdrop = native.prepare_window(self, self.settings.value("ui/backdrop", "gradient", str))
        self._shown = False

        self._build_menu()
        backdrop = QWidget()  # transparent: the window paints the backdrop behind menus too
        root = QHBoxLayout(backdrop)
        root.setContentsMargins(18, 8, 18, 12)
        root.setSpacing(18)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_content(), 1)
        self.setCentralWidget(backdrop)
        self.statusBar().showMessage(self._mode_text())
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _: self._apply_theme())
        self._refresh_datasets()
        if self.settings.value("updates/auto", True, bool):
            QTimer.singleShot(3000, lambda: self.updates.check(silent=True))

    # -- layout ---------------------------------------------------------------

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        self.export_posts = QAction("Export posts as CSV…", self, triggered=lambda: self._export("posts"))
        self.export_comments = QAction("Export comments as CSV…", self, triggered=lambda: self._export("comments"))
        file_menu.addAction(self.export_posts)
        file_menu.addAction(self.export_comments)
        file_menu.addSeparator()
        # On macOS Qt moves the Preferences, Quit and About roles into the application menu.
        file_menu.addAction(QAction("Reddit API settings…", self, shortcut=QKeySequence.StandardKey.Preferences,
                                    menuRole=QAction.MenuRole.PreferencesRole, triggered=self._open_settings))
        file_menu.addSeparator()
        file_menu.addAction(QAction("Quit", self, shortcut=QKeySequence.StandardKey.Quit,
                                    menuRole=QAction.MenuRole.QuitRole, triggered=self.close))

        view = self.menuBar().addMenu("&View")
        group = QActionGroup(self)
        current = self.settings.value("ui/theme", "system", str)
        for key, text in (("system", "Match system theme"), ("light", "Light"), ("dark", "Dark")):
            act = QAction(text, self, checkable=True, checked=key == current)
            act.triggered.connect(lambda _=False, k=key: self._set_theme(k))
            group.addAction(act)
            view.addAction(act)
        view.addSeparator()
        self.reduce_act = QAction("Reduce transparency", self, checkable=True,
                                  checked=self.settings.value("ui/reduce_transparency", False, bool))
        self.reduce_act.toggled.connect(self._set_reduce)
        view.addAction(self.reduce_act)

        options = native.backdrop_options()
        if len(options) > 1:  # Linux has only the painted gradient, so no menu there
            bg_menu = view.addMenu("Window background")
            bg_group = QActionGroup(self)
            current_bg = self.settings.value("ui/backdrop", "gradient", str)
            if current_bg not in {o.key for o in options}:
                current_bg = native.GRADIENT
            for opt in options:
                act = QAction(opt.label, self, checkable=True, checked=opt.key == current_bg)
                if opt.unavailable:
                    act.setEnabled(False)
                    act.setToolTip(opt.unavailable)
                act.triggered.connect(lambda _=False, k=opt.key: self._set_backdrop(k))
                bg_group.addAction(act)
                bg_menu.addAction(act)
            bg_menu.setToolTipsVisible(True)

        help_menu = self.menuBar().addMenu("&Help")
        help_menu.addAction(QAction("About ikari", self, menuRole=QAction.MenuRole.AboutRole,
                                    triggered=self._show_about))
        help_menu.addAction(QAction("Check for updates…", self, triggered=lambda: self.updates.check()))
        auto = QAction("Check for updates at startup", self, checkable=True,
                       checked=self.settings.value("updates/auto", True, bool))
        auto.toggled.connect(lambda on: self.settings.setValue("updates/auto", on))
        help_menu.addAction(auto)

    def _build_sidebar(self) -> QWidget:
        side, lay = panel("true", margins=20, spacing=14)
        side.setFixedWidth(318)

        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand.addWidget(label("ikari", "title"))
        brand.addWidget(label("Gather posts and read the mood", "subtitle"))
        lay.addLayout(brand)
        lay.addSpacing(6)

        self.subreddit = QLineEdit()
        self.subreddit.setPlaceholderText("python")
        self.subreddit.returnPressed.connect(self._start_fetch)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Optional keywords")
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

        lay.addWidget(section("Fetch"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.addRow(label("Subreddit", "muted"), self.subreddit)
        form.addRow(label("Search", "muted"), self.query)
        form.addRow(label("Sort", "muted"), self.sort)
        form.addRow(label("Time range", "muted"), self.time_filter)
        form.addRow(label("Max posts", "muted"), self.limit)
        form.addRow(self.include_comments)
        form.addRow(label("Per post", "muted"), self.comments_per_post)
        lay.addLayout(form)

        self.fetch_btn = mark(QPushButton("Fetch and analyse"), "primary", True)
        self.fetch_btn.setDefault(True)
        self.fetch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_btn.clicked.connect(self._start_fetch)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_fetch)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addWidget(self.fetch_btn, 1)
        buttons.addWidget(self.stop_btn)
        lay.addLayout(buttons)
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.progress_msg = label("", "muted")
        self.progress_msg.setWordWrap(True)
        lay.addWidget(self.progress)
        lay.addWidget(self.progress_msg)

        lay.addSpacing(4)
        lay.addWidget(section("Saved data"))
        self.datasets = QListWidget()
        self.datasets.setFrameShape(QFrame.Shape.NoFrame)
        self.datasets.currentItemChanged.connect(
            lambda item, _: self._show_dataset(item.data(Qt.ItemDataRole.UserRole) if item else ""))
        lay.addWidget(self.datasets, 1)
        self.delete_btn = mark(QPushButton("Delete selected"), "flat", True)
        self.delete_btn.clicked.connect(self._delete_dataset)
        lay.addWidget(self.delete_btn, 0, Qt.AlignmentFlag.AlignLeft)
        return side

    def _build_content(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.heading = label("No data yet", "heading")
        self.subheading = label("Enter a subreddit on the left and press Fetch and analyse.", "subtitle")
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.heading)
        titles.addWidget(self.subheading)

        self.tabbar = QTabBar()
        self.tabbar.setDrawBase(False)
        self.tabbar.setExpanding(False)
        for t in ("Overview", "Posts", "Comments"):
            self.tabbar.addTab(t)
        self.scope = QComboBox()
        self.scope.addItems(["Posts and comments", "Posts only", "Comments only"])
        self.scope.currentTextChanged.connect(lambda _: self._render_overview())
        header = QHBoxLayout()
        header.addLayout(titles, 1)
        header.addWidget(self.tabbar, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(header)

        self.pages = QStackedWidget()
        self.tabbar.currentChanged.connect(self.pages.setCurrentIndex)
        self.pages.addWidget(self._build_overview())
        self.posts_tab = TablePage(POST_COLUMNS, stretch_col=2)
        self.comments_tab = TablePage(COMMENT_COLUMNS, stretch_col=2)
        self.pages.addWidget(self.posts_tab)
        self.pages.addWidget(self.comments_tab)
        lay.addWidget(self.pages, 1)
        return content

    def _build_overview(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.tiles = {
            "count": StatTile("Items analysed"),
            "mean": StatTile("Mean sentiment"),
            "positive": StatTile("Positive", meter="positive"),
            "negative": StatTile("Negative", meter="negative"),
        }
        tiles = QHBoxLayout()
        tiles.setSpacing(14)
        for t in self.tiles.values():
            tiles.addWidget(t)
        lay.addLayout(tiles)

        self.distribution = DistributionChart()
        self.timeline = TimelineChart()
        charts = QHBoxLayout()
        charts.setSpacing(14)
        for chart, stretch in ((self.distribution, 2), (self.timeline, 3)):
            card, inner = panel("true", margins=10)
            inner.addWidget(chart)
            charts.addWidget(card, stretch)
        lay.addLayout(charts, 3)

        scope_row = QHBoxLayout()
        scope_row.addWidget(section("Strongest posts and comments"), 1)
        scope_row.addWidget(label("Include", "muted"))
        scope_row.addWidget(self.scope)
        lay.addLayout(scope_row)

        self.most_positive = QListWidget()
        self.most_negative = QListWidget()
        extremes = QHBoxLayout()
        extremes.setSpacing(14)
        for title, lw in (("Most positive", self.most_positive), ("Most negative", self.most_negative)):
            lw.setFrameShape(QFrame.Shape.NoFrame)
            lw.itemDoubleClicked.connect(
                lambda item: QDesktopServices.openUrl(QUrl(item.data(Qt.ItemDataRole.UserRole))))
            card, inner = panel("solid", margins=14, spacing=6)
            inner.addWidget(label(title, "muted"))
            inner.addWidget(lw)
            extremes.addWidget(card)
        lay.addLayout(extremes, 2)
        return page

    # -- theme ----------------------------------------------------------------

    def _set_theme(self, key: str) -> None:
        self.settings.setValue("ui/theme", key)
        self._apply_theme()

    def _set_reduce(self, on: bool) -> None:
        self.settings.setValue("ui/reduce_transparency", on)
        self._apply_theme()

    def _set_backdrop(self, key: str) -> None:
        self.settings.setValue("ui/backdrop", key)
        self.statusBar().showMessage("Restart the app to change the window background.", 10000)

    def _style_native_frame(self) -> None:
        t = theme.tokens()
        dark = theme.mode() == "dark"
        if self._native_backdrop:
            native.style_title_bar(self, dark)
        else:
            native.style_title_bar(self, dark, caption=t["bg0"], text=t["text"])

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            if self._native_backdrop and not native.apply_backdrop(self, self._native_backdrop):
                # Material refused: fall back to the painted gradient (opaque paint covers the translucency).
                self._native_backdrop = None
                self.update()
            self._style_native_frame()

    def _apply_theme(self) -> None:
        requested = self.settings.value("ui/theme", "system", str)
        native.set_appearance(requested)
        theme.apply(QApplication.instance(), requested, self.settings.value("ui/reduce_transparency", False, bool))
        self.update()
        self._style_native_frame()
        self._show_dataset(self.current or "")  # re-pick chart and swatch colors

    # -- control state --------------------------------------------------------

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
            return "Connected with Reddit API credentials."
        where = "ikari → Settings" if native.platform_name() == "macos" else "File → Reddit API settings"
        return f"Anonymous mode (slow). Add API credentials under {where} for faster, reliable fetching."

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
            item = QListWidgetItem(f"r/{n}")
            item.setData(Qt.ItemDataRole.UserRole, n)
            self.datasets.addItem(item)
        self.datasets.blockSignals(False)
        match = next((i for i, n in enumerate(names) if select and n.lower() == select.lower()), None)
        if match is not None:
            self.datasets.setCurrentRow(match)  # emits currentItemChanged -> _show_dataset
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
        self.tabbar.setTabText(1, f"Posts  {len(self.posts):,}")
        self.tabbar.setTabText(2, f"Comments  {len(self.comments):,}")
        if self.current:
            self.heading.setText(f"r/{self.current}")
            fetched = max((r["fetched_at"] for r in self.posts), default=None)
            when = datetime.fromtimestamp(fetched).strftime("%d %b %Y, %H:%M") if fetched else "never"
            self.subheading.setText(f"{len(self.posts):,} posts and {len(self.comments):,} comments · last fetched {when}")
        else:
            self.heading.setText("No data yet")
            self.subheading.setText("Enter a subreddit on the left and press Fetch and analyse.")
        self._render_overview()

    def _render_overview(self) -> None:
        scope = self.scope.currentText()
        rows = (list(self.posts) if scope != "Comments only" else []) + \
               (list(self.comments) if scope != "Posts only" else [])
        s = summarize(rows)
        n = s["count"]
        pos, neg = (s["counts"]["positive"] / n, s["counts"]["negative"] / n) if n else (0.0, 0.0)
        self.tiles["count"].set(f"{n:,}")
        self.tiles["mean"].set(f"{s['mean']:+.3f}" if n else "–")
        self.tiles["positive"].set(f"{100 * pos:.0f}%" if n else "–", pos)
        self.tiles["negative"].set(f"{100 * neg:.0f}%" if n else "–", neg)
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
            item = QListWidgetItem(f"{r['sentiment']:+.2f}    {text[:140]}")
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

    def _show_about(self) -> None:
        QMessageBox.about(self, "About ikari", (
            f"<h3>ikari {__version__}</h3>"
            "<p>Gather subreddit posts and comments and score their sentiment with VADER.</p>"
            "<p>Fonts: Manrope and Sora, both under the SIL Open Font License 1.1.</p>"))

    def paintEvent(self, event) -> None:
        paint_backdrop(self, over_native=self._native_backdrop is not None)

    def closeEvent(self, event) -> None:
        if self.worker:
            self.worker.stop()
            self.thread.quit()
            self.thread.wait(3000)
        self.store.close()
        super().closeEvent(event)
