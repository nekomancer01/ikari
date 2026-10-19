from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import (
    QDate, QEvent, QRectF, QSettings, QSize, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout, QFrame,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QStackedWidget, QTableView, QTabBar,
    QVBoxLayout, QWidget,
)

from subsentiment import __version__, collect
from subsentiment.gui import native, theme
from subsentiment.gui.charts import DistributionChart, TimelineChart, palette
from subsentiment.gui.models import COMMENT_COLUMNS, POST_COLUMNS, SORT_ROLE, RowsModel
from subsentiment.gui.theme import mark, paint_backdrop
from subsentiment.gui.update_flow import UpdateFlow
from subsentiment.gui.worker import FetchWorker, LiveWorker
from subsentiment.reddit import normalize_subreddit, subreddit_error
from subsentiment.store import Store, daily_means, summarize


# Date ranges the fetch form offers: key -> (label, days back from now). "custom" uses the From/To dates.
RANGES = {"day": ("Past 24 hours", 1), "week": ("Past week", 7), "month": ("Past 30 days", 30),
          "quarter": ("Past 3 months", 91), "year": ("Past year", 365), "custom": ("Custom dates", None)}

# Last-used fetch form, restored at startup. Keys live under "fetch/" in QSettings.
FETCH_DEFAULTS = {"subreddit": "", "query": "", "range": "week", "from": "", "to": "", "pick": "spread",
                  "limit": 100, "include_comments": True, "comments_per_post": 50, "refresh_scores": False}

SCOPES = ["Posts and comments", "Posts only", "Comments only"]

PICKS = {"spread": ("Spread across days",
                    "An equal share of posts from each part of the range. Fetching again adds posts you "
                    "don't have yet."),
         "top": ("Top by score",
                 "The highest-scored posts from each part of the range. Scores only exist once the archive "
                 "re-checks a post (usually 36 hours after posting, sometimes up to a week), so the newest "
                 "posts can't be ranked yet.")}


def range_bounds(key: str, start: QDate, end: QDate, now: datetime | None = None) -> tuple[float, float | None]:
    """(after, before) in epoch seconds for a range key; before is None for "up to now"."""
    now = now or datetime.now()
    days = RANGES[key][1]
    if days is not None:
        return (now - timedelta(days=days)).timestamp(), None
    lo, hi = sorted((start, end))
    first = datetime(lo.year(), lo.month(), lo.day())
    last = datetime(hi.year(), hi.month(), hi.day()) + timedelta(days=1)  # "to" includes that whole day
    return first.timestamp(), last.timestamp()


def header_font(base: QFont) -> QFont:
    """The table-header font the stylesheet draws with (8.5pt semibold)."""
    font = QFont(base)
    font.setPointSizeF(8.5)
    font.setWeight(QFont.Weight.DemiBold)
    return font


def date_edit() -> QDateEdit:
    edit = QDateEdit()
    edit.setCalendarPopup(True)
    edit.setDisplayFormat("d MMM yyyy")
    edit.setMinimumDate(QDate(2005, 6, 1))  # Reddit's first month
    edit.setMaximumDate(QDate.currentDate())
    edit.setMinimumWidth(110)  # its size hint is wide enough to squeeze the form's labels
    return edit


def spin_box(lo: int, hi: int, step: int, value: int, suffix: str = "") -> QSpinBox:
    spin = QSpinBox()
    spin.setRange(lo, hi)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setSuffix(suffix)
    # The stylesheet's horizontal padding doesn't reach a spin box's inner editor; match the other inputs.
    spin.findChild(QLineEdit).setTextMargins(8, 0, 0, 0)
    return spin


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:,.0f}\u00a0{unit}" if unit == "bytes" else f"{n:,.1f}\u00a0{unit}"
        n /= 1024
    return ""


def fit_to_screen(size: QSize, screen) -> QSize:
    """``size``, shrunk to fit the screen's free area (a 1920x1200 screen at 150% has about 1280x750)."""
    if screen is None:
        return size
    free = screen.availableGeometry()
    return QSize(min(size.width(), int(free.width() * 0.94)), min(size.height(), int(free.height() * 0.92)))


def finish_text(result: collect.FetchResult, req: collect.FetchRequest) -> str:
    """What a finished fetch did, for the line under the progress bar."""
    if req.comment_posts:
        parts = [f"Added {plural(result.comments, 'comment')} on {plural(len(req.comment_posts), 'post')} "
                 f"from r/{result.subreddit}."]
        if result.comment_errors:
            parts.append(f"Comments couldn't be fetched for {plural(result.comment_errors, 'post')}; "
                         "try again later.")
        if result.stopped:
            parts.append("Stopped early.")
        return " ".join(parts)
    if not (result.posts or result.updated or result.already):
        if result.stopped:
            return "Stopped before any posts were fetched."
        matching = f" matching \u201c{req.query}\u201d" if req.query else ""
        return f"No new posts found in r/{result.subreddit}{matching} in this date range."
    parts = [f"Added {plural(result.posts, 'new post')}"
             + (f" and {plural(result.comments, 'comment')}" if req.include_comments else "")
             + f" from r/{result.subreddit}."]
    if result.updated:
        parts.append(f"Updated scores on {plural(result.updated, 'saved post')}.")
    if result.already:
        parts.append(f"{plural(result.already, 'top post')} {'was' if result.already == 1 else 'were'} "
                     "already saved.")
    if result.comment_errors:
        parts.append(f"Comments couldn't be fetched for {plural(result.comment_errors, 'post')}; "
                     "fetch again later to fill them in.")
    if result.stopped:
        parts.append("Stopped early.")
    return " ".join(parts)


def age_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return "under a minute" if seconds < 60 else "1 minute"
    if seconds < 5400:
        return f"{round(seconds / 60)} minutes"
    return f"{seconds / 3600:.1f} hours" if seconds < 172800 else f"{seconds // 86400} days"


def plural(n: int, noun: str) -> str:
    return f"{n:,}\u00a0{noun}{'' if n == 1 else 's'}"  # no-break space keeps a number with its noun


def when_text(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y, %H:%M") if ts else "never"


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
        self.stretch_col = stretch_col
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
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

    def load(self, rows, animate: bool = False) -> None:
        self._all_rows = rows
        self._apply_label_filter(self.label_filter.currentText(), animate)

    def _apply_label_filter(self, text: str, animate: bool = False) -> None:
        rows = self._all_rows
        if text != "All labels":
            rows = [r for r in rows if r["label"] == text.lower()]
        self.model.set_rows(rows, animate)
        if not animate:
            self._fit_columns()
        self._update_count()

    def _fit_columns(self) -> None:
        """Size each column to its values and its full header title, leaving the rest to the text column.

        Qt's own sizing leaves out room for the sort arrow, so a sorted column's title got cut short."""
        header = self.view.horizontalHeader()
        font = header_font(self.view.font())
        header.setFont(font)  # Qt elides header titles using the widget's font, not the stylesheet's
        metrics = QFontMetrics(font)
        for col in range(self.model.columnCount()):
            if col == self.stretch_col:
                continue
            title = self.model.headerData(col, Qt.Orientation.Horizontal)
            header.resizeSection(col, max(self.view.sizeHintForColumn(col),
                                          metrics.horizontalAdvance(title) + 16 + 24 + 8))  # padding, arrow, slack
    def _update_count(self, *args) -> None:
        self.count.setText(f"{self.proxy.rowCount():,} rows")

    def _open(self, index) -> None:
        row = self.model.row_at(self.proxy.mapToSource(index).row())
        QDesktopServices.openUrl(QUrl(row["permalink"]))


class MainWindow(QMainWindow):
    def __init__(self, db_path: str):
        super().__init__()
        self.setWindowTitle("ikari")
        self.resize(fit_to_screen(QSize(1320, 860), QGuiApplication.primaryScreen()))
        self.db_path = db_path
        self.store = Store(db_path)
        self.settings = QSettings()
        self.updates = UpdateFlow(self, self.settings)
        self.thread: QThread | None = None
        self.worker: FetchWorker | None = None
        self.current: str | None = None
        self.request: collect.FetchRequest | None = None
        self._live = QTimer(self)
        self._live.setSingleShot(True)
        self._live.setInterval(1000)
        self._live.timeout.connect(self._live_refresh)
        self.live_thread: QThread | None = None
        self.live_worker: LiveWorker | None = None
        self.live_name: str | None = None
        self._live_seen = 0
        self.posts: list = []
        self.comments: list = []
        self.coverage = None  # the Download coverage window, once opened
        self.setWindowIcon(QIcon(str(theme.ASSETS / "app.png")))
        # A native material (Mica/Acrylic/vibrancy) needs a translucent window from the start,
        # so a change applies on next launch.
        self._native_backdrop = native.prepare_window(self, self.settings.value("ui/backdrop", "gradient", str))
        self._shown = False
        self.mood = theme.MoodTint(self, self.settings.value("ui/mood_tint", True, bool))

        self._build_menu()
        backdrop = QWidget()  # transparent: the window paints the backdrop behind menus too
        root = QHBoxLayout(backdrop)
        root.setContentsMargins(18, 8, 18, 12)
        root.setSpacing(18)
        root.addWidget(self._build_sidebar())
        root.addWidget(self._build_content(), 1)
        self.setCentralWidget(backdrop)
        # Permanent, so temporary messages (exports, restarts) don't wipe the access-mode note.
        # The status bar only appears while it has something to say (an export, a deletion).
        bar = self.statusBar()
        bar.messageChanged.connect(lambda text: bar.setVisible(bool(text)))
        bar.hide()
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _: self._apply_theme())
        self._refresh_datasets(select=self.settings.value("ui/last_dataset", "", str))
        self.tabbar.setCurrentIndex(self.settings.value("ui/tab", 0, int))
        self.tabbar.currentChanged.connect(lambda i: self.settings.setValue("ui/tab", i))
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
        self.delete_act = QAction("Delete saved data for this subreddit…", self, triggered=self._delete_dataset)
        self.delete_all_act = QAction("Delete all saved data…", self, triggered=self._delete_all)
        file_menu.addAction(self.delete_act)
        file_menu.addAction(self.delete_all_act)
        file_menu.addSeparator()
        # On macOS Qt moves the Quit and About roles into the application menu.
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
        self.mood_act = QAction("Tint background by sentiment", self, checkable=True, checked=self.mood.enabled)
        self.mood_act.toggled.connect(self._set_mood_tint)
        view.addAction(self.mood_act)
        view.addSeparator()
        self.coverage_act = QAction("Download coverage…", self, shortcut=QKeySequence("Ctrl+D"),
                                    triggered=self._show_coverage)
        self.coverage_act.setToolTip("How much of each day's posts, and each post's comments, is saved")
        view.addAction(self.coverage_act)

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
        side, outer = panel("true", margins=1, spacing=0)
        side.setFixedWidth(318)
        # On a short window (a 1080p screen at 150% leaves about 700 px) the sidebar scrolls rather than
        # squashing its fields.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(19, 19, 19, 19)
        lay.setSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll)

        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand.addWidget(label("ikari", "title"))
        brand.addWidget(label("Gather posts and read the mood", "subtitle"))
        lay.addLayout(brand)
        lay.addSpacing(6)

        self.subreddit = QLineEdit()
        self.subreddit.setPlaceholderText("e.g. python")
        self.subreddit.setToolTip("A subreddit name; r/name and reddit.com links work too.")
        self.subreddit.returnPressed.connect(self._start_fetch)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Optional keywords")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self._start_fetch)
        self.range = QComboBox()
        for key, (text, _) in RANGES.items():
            self.range.addItem(text, key)
        self.range.currentIndexChanged.connect(self._on_range_changed)
        self.date_from = date_edit()
        self.date_to = date_edit()
        for edit in (self.date_from, self.date_to):
            edit.userDateChanged.connect(self._on_date_edited)
        self.pick = QComboBox()
        for i, (key, (text, tip)) in enumerate(PICKS.items()):
            self.pick.addItem(text, key)
            self.pick.setItemData(i, tip, Qt.ItemDataRole.ToolTipRole)
        self.pick.currentIndexChanged.connect(
            lambda _: self.pick.setToolTip(PICKS[self.pick.currentData()][1]))
        self.limit = spin_box(1, 1000, 25, FETCH_DEFAULTS["limit"])
        self.include_comments = QCheckBox("Fetch")
        self.comments_per_post = spin_box(1, 500, 10, FETCH_DEFAULTS["comments_per_post"], " per post")
        self.comments_per_post.setMinimumWidth(100)  # its size hint would squeeze the form's labels
        self.include_comments.toggled.connect(lambda _: self._update_comments_enabled())
        self.refresh_scores = QCheckBox("Update saved posts")
        self.refresh_scores.setToolTip("Also re-check saved posts in this range whose scores and comment counts "
                                       "hadn't come in yet. Takes extra requests, so it's off by default.")
        comments_row = QWidget()
        row = QHBoxLayout(comments_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)
        row.addWidget(self.include_comments)
        row.addWidget(self.comments_per_post, 1)
        self._restore_fetch_settings()

        lay.addWidget(section("Fetch"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.addRow(label("Subreddit", "muted"), self.subreddit)
        form.addRow(label("Search", "muted"), self.query)
        form.addRow(label("Posted", "muted"), self.range)
        form.addRow(label("From", "muted"), self.date_from)
        form.addRow(label("To", "muted"), self.date_to)
        form.addRow(label("Pick", "muted"), self.pick)
        form.addRow(label("Max posts", "muted"), self.limit)
        form.addRow(label("Comments", "muted"), comments_row)
        form.addRow(label("Scores", "muted"), self.refresh_scores)
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
        self.live_btn = QPushButton("Live feed")
        self.live_btn.setCheckable(True)
        self.live_btn.setToolTip("Keep checking Arctic Shift for new posts in this subreddit and add them as they appear")
        self.live_btn.toggled.connect(self._toggle_live)
        lay.addWidget(self.live_btn)
        self.live_msg = label("", "muted")
        self.live_msg.setWordWrap(True)
        self.live_msg.setVisible(False)
        lay.addWidget(self.live_msg)
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
        self.datasets.currentItemChanged.connect(self._on_dataset_selected)
        lay.addWidget(self.datasets, 1)
        self.storage_info = label("", "muted")
        self.storage_info.setWordWrap(True)
        lay.addWidget(self.storage_info)
        self.delete_btn = mark(QPushButton("Delete…"), "flat", True)
        self.delete_btn.setToolTip("Delete the saved posts and comments for the selected subreddit")
        self.delete_btn.clicked.connect(self._delete_dataset)
        self.delete_all_btn = mark(QPushButton("Delete all…"), "flat", True)
        self.delete_all_btn.setToolTip("Delete every saved subreddit")
        self.delete_all_btn.clicked.connect(self._delete_all)
        delete_row = QHBoxLayout()
        delete_row.setSpacing(4)
        delete_row.addWidget(self.delete_btn)
        delete_row.addWidget(self.delete_all_btn)
        delete_row.addStretch(1)
        lay.addLayout(delete_row)
        return side

    def _build_content(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.heading = label("No data yet", "heading")
        self.subheading = label("Enter a subreddit on the left and press Fetch and analyse.", "subtitle")
        self.subheading.setWordWrap(True)  # wraps on a narrow window instead of widening it
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(self.heading)
        titles.addWidget(self.subheading)

        self.tabbar = QTabBar()
        self.tabbar.setDrawBase(False)
        self.tabbar.setExpanding(False)
        self.tabbar.setUsesScrollButtons(False)  # all three tabs always show; the window is never narrower
        for t in ("Overview", "Posts", "Comments"):
            self.tabbar.addTab(t)
        # What the whole Overview (tiles, charts, strongest lists, mood tint) counts.
        self.scope = QComboBox()
        self.scope.addItems(SCOPES)
        self.scope.setToolTip("Count posts, comments or both on the Overview")
        saved = self.settings.value("ui/overview_scope", SCOPES[0], str)
        self.scope.setCurrentIndex(max(self.scope.findText(saved), 0))
        self.scope.currentTextChanged.connect(self._on_scope_changed)
        header = QHBoxLayout()
        header.setSpacing(12)
        header.addLayout(titles, 1)
        header.addWidget(self.scope, 0, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.tabbar, 0, Qt.AlignmentFlag.AlignBottom)
        lay.addLayout(header)

        self.pages = QStackedWidget()
        self.tabbar.currentChanged.connect(self.pages.setCurrentIndex)
        self.tabbar.currentChanged.connect(lambda i: self.scope.setVisible(i == 0))
        self.pages.addWidget(self._build_overview())
        self.posts_tab = TablePage(POST_COLUMNS, stretch_col=2)
        self.comments_tab = TablePage(COMMENT_COLUMNS, stretch_col=2)
        # The post title would otherwise size to its text and squeeze the comment column.
        post_col = self.comments_tab.view.horizontalHeader()
        post_col.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        post_col.resizeSection(4, 180)
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

        self.most_positive = QListWidget()
        self.most_negative = QListWidget()
        extremes = QHBoxLayout()
        extremes.setSpacing(14)
        for title, lw in (("Most positive", self.most_positive), ("Most negative", self.most_negative)):
            lw.setFrameShape(QFrame.Shape.NoFrame)
            # Long titles end in "…" (the tooltip has the rest) instead of adding a sideways scrollbar.
            lw.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            lw.setTextElideMode(Qt.TextElideMode.ElideRight)
            lw.setUniformItemSizes(True)
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

    def _set_mood_tint(self, on: bool) -> None:
        self.settings.setValue("ui/mood_tint", on)
        self.mood.set_enabled(on)

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

    def _on_range_changed(self) -> None:
        """A preset fills in the dates it covers, so the From and To boxes always show the range."""
        days = RANGES[self.range.currentData()][1]
        if days is not None:
            today = QDate.currentDate()
            for edit, date in ((self.date_from, today.addDays(-days)), (self.date_to, today)):
                edit.blockSignals(True)
                edit.setMaximumDate(today)
                edit.setDate(date)
                edit.blockSignals(False)

    def _on_date_edited(self) -> None:
        """Typing or picking a date means a custom range."""
        self.range.blockSignals(True)
        self.range.setCurrentIndex(self.range.findData("custom"))
        self.range.blockSignals(False)

    def _update_comments_enabled(self) -> None:
        self.comments_per_post.setEnabled(self.thread is None and self.include_comments.isChecked())

    def _restore_fetch_settings(self) -> None:
        v = {k: self.settings.value(f"fetch/{k}", d, type(d)) for k, d in FETCH_DEFAULTS.items()}
        self.subreddit.setText(v["subreddit"])
        self.query.setText(v["query"])
        self.range.setCurrentIndex(max(self.range.findData(v["range"]), 0))
        self._on_range_changed()
        if self.range.currentData() == "custom":
            for edit, text in ((self.date_from, v["from"]), (self.date_to, v["to"])):
                date = QDate.fromString(text, Qt.DateFormat.ISODate)
                if date.isValid():
                    edit.blockSignals(True)
                    edit.setDate(date)
                    edit.blockSignals(False)
        self.pick.setCurrentIndex(max(self.pick.findData(v["pick"]), 0))
        self.pick.setToolTip(PICKS[self.pick.currentData()][1])
        self.limit.setValue(v["limit"])
        self.include_comments.setChecked(v["include_comments"])
        self.refresh_scores.setChecked(v["refresh_scores"])
        self.comments_per_post.setValue(v["comments_per_post"])
        self._update_comments_enabled()

    def _save_fetch_settings(self) -> None:
        for k, v in {"subreddit": self.subreddit.text().strip(), "query": self.query.text().strip(),
                     "range": self.range.currentData(),
                     "from": self.date_from.date().toString(Qt.DateFormat.ISODate),
                     "to": self.date_to.date().toString(Qt.DateFormat.ISODate),
                     "pick": self.pick.currentData(),
                     "limit": self.limit.value(), "include_comments": self.include_comments.isChecked(),
                     "refresh_scores": self.refresh_scores.isChecked(),
                     "comments_per_post": self.comments_per_post.value()}.items():
            self.settings.setValue(f"fetch/{k}", v)

    def _set_busy(self, busy: bool) -> None:
        # Called after self.thread is set (busy) or cleared (idle); the update helpers read it.
        self.fetch_btn.setEnabled(not busy and self.live_thread is None)
        self.live_btn.setEnabled(not busy)
        self.stop_btn.setEnabled(busy)
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        for w in (self.subreddit, self.query, self.range, self.date_from, self.date_to, self.pick,
                  self.limit, self.include_comments, self.refresh_scores):
            w.setEnabled(not busy)
        self._update_comments_enabled()
        self._update_delete_enabled()  # don't delete rows the fetch thread is writing
        if self.coverage is not None:
            self.coverage.set_busy(busy)

    def _update_delete_enabled(self) -> None:
        idle = self.thread is None
        self.delete_btn.setEnabled(idle and bool(self.current))
        self.delete_act.setEnabled(idle and bool(self.current))
        self.delete_all_btn.setEnabled(idle and self.datasets.count() > 0)
        self.delete_all_act.setEnabled(idle and self.datasets.count() > 0)

    # -- fetching -------------------------------------------------------------

    def _start_fetch(self) -> None:
        if self.thread is not None or self.live_thread is not None:
            return
        name = normalize_subreddit(self.subreddit.text())
        if error := subreddit_error(name):
            self.progress_msg.setText(error)
            self.subreddit.setFocus()
            self.subreddit.selectAll()
            return
        self.subreddit.setText(name)
        self._save_fetch_settings()
        self._on_range_changed()  # a preset counts back from now, not from when the app started
        after, before = range_bounds(self.range.currentData(), self.date_from.date(), self.date_to.date())
        req = collect.FetchRequest(
            subreddit=name, after=after, before=before, limit=self.limit.value(), query=self.query.text().strip(),
            include_comments=self.include_comments.isChecked(), refresh_scores=self.refresh_scores.isChecked(),
            comments_per_post=self.comments_per_post.value(), top=self.pick.currentData() == "top")
        self._launch(req, f"Contacting Arctic Shift for r/{name}…")

    def start_request(self, req: collect.FetchRequest, message: str) -> bool:
        """Run a fetch the coverage panel asked for; False if one is already running."""
        if self.thread is not None:
            return False
        self._launch(req, message)
        return True

    def _launch(self, req: collect.FetchRequest, message: str) -> None:
        self.request = req
        self.thread = QThread(self)
        self.worker = FetchWorker(req, self.db_path)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.finished.connect(self._on_finished)
        self.worker.saved.connect(self._on_saved)
        self.worker.failed.connect(self._on_failed)
        self.worker.finished.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.thread.finished.connect(self._on_thread_done)
        self._set_busy(True)
        self.progress_msg.setText(message)
        self.thread.start()

    # -- live feed ------------------------------------------------------------

    def _toggle_live(self, on: bool) -> None:
        if on:
            self._start_live()
        else:
            self._stop_live()

    def _start_live(self) -> None:
        name = normalize_subreddit(self.subreddit.text())
        if error := subreddit_error(name):
            self.progress_msg.setText(error)
            self.live_btn.blockSignals(True)
            self.live_btn.setChecked(False)
            self.live_btn.blockSignals(False)
            self.subreddit.setFocus()
            return
        self.subreddit.setText(name)
        self.live_name = name
        self._live_seen = 0
        self.live_thread = QThread(self)
        self.live_worker = LiveWorker(name, self.db_path)
        w = self.live_worker
        w.moveToThread(self.live_thread)
        self.live_thread.started.connect(w.run)
        w.status.connect(self._set_live_msg)
        w.polled.connect(self._on_live_polled)
        w.saved.connect(self._on_live_saved)
        w.failed.connect(lambda m: QMessageBox.warning(self, "Live feed stopped", m))
        w.finished.connect(self.live_thread.quit)
        self.live_thread.finished.connect(self._on_live_done)
        for widget in (self.subreddit, self.fetch_btn):
            widget.setEnabled(False)
        self.live_msg.setVisible(True)
        self._set_live_msg(f"Live: checking r/{name} for new posts…")
        self.live_thread.start()

    def _stop_live(self) -> None:
        if self.live_worker:
            self.live_worker.stop()
            self.live_btn.setEnabled(False)
            self._set_live_msg("Stopping the live feed…")

    def _on_live_done(self) -> None:
        self.live_thread.deleteLater()
        self.live_worker.deleteLater()
        self.live_thread = self.live_worker = None
        self.subreddit.setEnabled(True)
        self.fetch_btn.setEnabled(self.thread is None)
        self.live_btn.setEnabled(self.thread is None)
        self.live_btn.blockSignals(True)
        self.live_btn.setChecked(False)
        self.live_btn.blockSignals(False)
        self.live_msg.setVisible(False)

    def _set_live_msg(self, text: str) -> None:
        self.live_msg.setText(text)

    def _on_live_polled(self, result) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        newest = (f" Newest post is {age_text(datetime.now().timestamp() - result.newest)} old."
                  if result.newest else "")
        self._set_live_msg(
            f"Live: r/{self.live_name}, {plural(self._live_seen + result.new, 'new post')} this session. "
            f"Checked {stamp}.{newest} Arctic Shift archives posts a little after Reddit, so the feed "
            "can trail by a few minutes.")

    def _on_live_saved(self, new: int, newest) -> None:
        self._live_seen += new
        name = self.live_name or ""
        showing = self.current is None or self.current.lower() == name.lower()
        self._refresh_datasets(select=None if self.current else name, reload=False)
        if showing:
            item = self.datasets.currentItem()
            shown = item.data(Qt.ItemDataRole.UserRole) if item else None
            if shown and shown.lower() == name.lower():
                self._show_dataset(shown, animate=self.current is not None)

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
        if self.coverage is not None:
            self.coverage.set_progress(done, total, msg)

    def _on_saved(self) -> None:
        # Rows arrive in bursts; redraw at most about once a second so big fetches stay smooth.
        if not self._live.isActive():
            self._live.start()

    def _live_refresh(self) -> None:
        if self.thread is None or self.request is None:
            return
        target = self.request.subreddit.lower()
        showing = self.current is None or self.current.lower() == target
        self._refresh_datasets(select=None if self.current else self.request.subreddit, reload=False)
        if showing:
            item = self.datasets.currentItem()
            name = item.data(Qt.ItemDataRole.UserRole) if item else None
            if name and name.lower() == target:
                self._show_dataset(name, animate=self.current is not None)
        if self.coverage is not None:
            self.coverage.refresh_saved()

    def _on_finished(self, result: collect.FetchResult) -> None:
        self.progress_msg.setText(finish_text(result, self.request))
        if self.coverage is not None:
            self.coverage.set_progress(0, 0, self.progress_msg.text())
        self._live.stop()
        if self.current and self.current.lower() == result.subreddit.lower():
            self._refresh_datasets(reload=False)
            self._show_dataset(self.current, animate=True)  # keeps the tables where they are
        elif result.posts or result.updated or result.comments:
            self._refresh_datasets(select=result.subreddit)

    def _on_failed(self, message: str) -> None:
        self._refresh_datasets(select=self.request.subreddit)  # show anything stored before the error
        kept = self.current and self.current.lower() == self.request.subreddit.lower()
        self.progress_msg.setText("Fetch failed. What was fetched before the error is saved." if kept
                                  else "Fetch failed.")
        QMessageBox.warning(self, "Fetch failed", message)

    def _on_thread_done(self) -> None:
        self.thread.deleteLater()
        self.worker.deleteLater()
        self.thread = self.worker = None
        self._set_busy(False)
        if self.coverage is not None:
            self.coverage.refresh_saved()

    # -- datasets -------------------------------------------------------------

    def _refresh_datasets(self, select: str | None = None, reload: bool = True) -> None:
        """Reload the saved-data list, selecting select, else the current dataset, else the newest.
        With reload=False only the list and storage line change, not the dataset on screen."""
        sets = self.store.datasets()
        names = [d["name"] for d in sets]
        self.datasets.blockSignals(True)
        self.datasets.clear()
        for d in sets:
            item = QListWidgetItem(f"r/{d['name']}")
            item.setData(Qt.ItemDataRole.UserRole, d["name"])
            item.setToolTip(f"{d['posts']:,} posts and {d['comments']:,} comments · "
                            f"last fetched {when_text(d['last_fetched'])}")
            self.datasets.addItem(item)
        lowered = [n.lower() for n in names]
        row = next((lowered.index(c.lower()) for c in (select, self.current) if c and c.lower() in lowered),
                   0 if names else None)
        if row is not None:
            self.datasets.setCurrentRow(row)
        self.datasets.blockSignals(False)
        if reload:
            self._on_dataset_selected(self.datasets.currentItem())  # reload even when the selection is unchanged
        posts, comments = sum(d["posts"] for d in sets), sum(d["comments"] for d in sets)
        self.storage_info.setText(
            f"{plural(len(sets), 'subreddit')}, {plural(posts, 'post')}, {plural(comments, 'comment')} · "
            f"{human_size(self.store.size_bytes())} on this computer" if sets
            else "Nothing saved yet. Fetched data is kept on this computer between sessions.")
        self._update_delete_enabled()

    def _on_dataset_selected(self, item: QListWidgetItem | None) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item else ""
        self._show_dataset(name)
        if name:
            self.settings.setValue("ui/last_dataset", name)

    def _show_dataset(self, name: str, animate: bool = False) -> None:
        """Show a saved subreddit. With animate (a fetch adding to it), new rows fade in."""
        self.current = name or None
        self.posts = self.store.posts(name) if name else []
        self.comments = self.store.comments(name) if name else []
        self.posts_tab.load(self.posts, animate)
        self.comments_tab.load(self.comments, animate)
        self.export_posts.setEnabled(bool(self.posts))
        self.export_comments.setEnabled(bool(self.comments))
        self._update_delete_enabled()
        self.tabbar.setTabText(1, f"Posts  {len(self.posts):,}")
        self.tabbar.setTabText(2, f"Comments  {len(self.comments):,}")
        if self.current:
            self.heading.setText(f"r/{self.current}")
            fetched = max((r["fetched_at"] for r in self.posts), default=None)
            self.subheading.setText(f"{plural(len(self.posts), 'post')} and {plural(len(self.comments), 'comment')} · "
                                    f"last fetched {when_text(fetched)}")
        else:
            self.heading.setText("No data yet")
            self.subheading.setText("Enter a subreddit on the left and press Fetch and analyse.")
        self._render_overview()
        self.coverage_act.setEnabled(bool(self.current))
        if self.coverage is not None:
            self.coverage.set_subreddit(self.current)

    def _on_scope_changed(self, text: str) -> None:
        self.settings.setValue("ui/overview_scope", text)
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
        self.mood.set_counts(s["counts"]["positive"], s["counts"]["negative"], n)
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
        if not self.current or self.thread is not None:
            return
        if QMessageBox.question(
                self, "Delete saved data",
                f"Delete the {len(self.posts):,} posts and {len(self.comments):,} comments saved for "
                f"r/{self.current}? This can't be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_subreddit(self.current)
        self.statusBar().showMessage(f"Deleted the saved data for r/{self.current}.", 8000)
        self.current = None
        self._refresh_datasets()

    def _delete_all(self) -> None:
        if self.thread is not None or not self.datasets.count():
            return
        if QMessageBox.question(
                self, "Delete all saved data",
                f"Delete everything saved for all {self.datasets.count()} subreddits? "
                "This can't be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_all()
        self.settings.remove("ui/last_dataset")
        self.statusBar().showMessage("Deleted all saved data.", 8000)
        self.current = None
        self._refresh_datasets()

    def _export(self, kind: str) -> None:
        if not self.current:
            return
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind}", f"{self.current}_{kind}.csv", "CSV files (*.csv)")
        if path:
            n = self.store.export_csv(self.current, kind, path)
            self.statusBar().showMessage(f"Exported {n} {kind} to {path}", 8000)

    def fetch_range(self) -> tuple[float, float]:
        """The sidebar's date range as (after, before), before being now for "up to now"."""
        after, before = range_bounds(self.range.currentData(), self.date_from.date(), self.date_to.date())
        return after, before if before is not None else datetime.now().timestamp()

    def _show_coverage(self) -> None:
        if not self.current:
            return
        if self.coverage is None:
            from subsentiment.gui.coverage import CoverageWindow  # it imports this module's helpers
            self.coverage = CoverageWindow(self)
        self.coverage.set_subreddit(self.current)
        self.coverage.set_busy(self.thread is not None)
        self.coverage.show()
        self.coverage.raise_()
        self.coverage.activateWindow()

    def _show_about(self) -> None:
        QMessageBox.about(self, "About ikari", (
            f"<h3>ikari {__version__}</h3>"
            "<p>Gather subreddit posts and comments and score their sentiment with VADER.</p>"
            "<p>Fonts: Manrope and Sora, both under the SIL Open Font License 1.1.</p>"))

    def paintEvent(self, event) -> None:
        paint_backdrop(self, over_native=self._native_backdrop is not None, mood=self.mood.state)

    def closeEvent(self, event) -> None:
        self._save_fetch_settings()
        if self.coverage is not None:
            self.coverage.close()
        if self.live_worker:
            self.live_worker.stop()
            self.live_thread.quit()
            self.live_thread.wait(3000)
        if self.worker:
            self.worker.stop()
            self.thread.quit()
            self.thread.wait(3000)
        self.store.close()
        super().closeEvent(event)
