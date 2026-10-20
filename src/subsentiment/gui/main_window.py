from __future__ import annotations

import sqlite3
import threading
import time
from datetime import datetime
from functools import partial

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QSize, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QFontMetrics, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from subsentiment import __version__, collect
from subsentiment.analysis import Analysis
from subsentiment.gui import days, motion, native, theme
from subsentiment.gui.coverage import CoveragePage
from subsentiment.gui.fetch_form import FetchForm
from subsentiment.gui.charts import NoteDialog, TimelineChart
from subsentiment.gui.overview import CardPage, OverviewPage
from subsentiment.gui.words import KINDS, WordCloud, WordTally
from subsentiment.gui.rail import DRAWERS, PAGES, NavRail
from subsentiment.gui.tables import COMMENT_COLUMNS, POST_COLUMNS, TablePage
from subsentiment.gui.theme import mark, paint_backdrop
from subsentiment.gui.update_flow import UpdateFlow
from subsentiment.gui.widgets import (
    ElidedLabel, QuietScrollBars, ShrinkingComboBox, StatusLabel, fit_to_screen, label, panel, plural, section,
)
from subsentiment.gui.worker import BackgroundJob, FetchWorker, LoadWorker, Slices
from subsentiment.reddit import normalize_subreddit, subreddit_error
from subsentiment.store import Store, scoring_at_least


SCOPES = ["Posts and comments", "Posts only", "Comments only"]
# The header's score filter: label -> the lowest post score shown (None for any). Comments follow their post.
MIN_SCORES = {"Any score": None, "Score 1+": 1, "Score 10+": 10, "Score 50+": 50, "Score 100+": 100,
              "Score 500+": 500, "Score 1,000+": 1000}
DRAWER_WIDTH = 318
NO_DATA = "Open Fetch on the left, enter a subreddit and press Fetch and analyse."


class FetchStrip(QWidget):
    """Under the header: the running fetch's progress, what it's doing and Stop, then what it did, until
    Hide. It takes no room while it has nothing to show."""

    def __init__(self):
        super().__init__()
        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setVisible(False)
        self.message = StatusLabel()
        self.stop_btn = mark(QPushButton("Stop"), "small", True)
        self.stop_btn.setEnabled(False)
        self.stop_btn.setVisible(False)
        self.hide_btn = mark(mark(QPushButton("Hide"), "flat", True), "small", True)
        self.hide_btn.clicked.connect(lambda: self.message.setText(""))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 0)
        lay.setSpacing(10)
        lay.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.message, 1)
        lay.addWidget(self.stop_btn)
        lay.addWidget(self.hide_btn)
        for w in (self.progress, self.message):
            w.installEventFilter(self)
        self.setVisible(False)

    def set_busy(self, busy: bool) -> None:
        self.stop_btn.setEnabled(busy)
        self.stop_btn.setVisible(busy)
        self.hide_btn.setVisible(not busy)
        self.progress.setVisible(busy)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.ShowToParent, QEvent.Type.HideToParent):
            self.setVisible(not (self.progress.isHidden() and self.message.isHidden()))
        return False


def human_size(n: int) -> str:
    for unit in ("bytes", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:,.0f}\u00a0{unit}" if unit == "bytes" else f"{n:,.1f}\u00a0{unit}"
        n /= 1024
    return ""


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
        if result.timed_out:
            return f"Arctic Shift timed out on r/{result.subreddit} in this range; try again in a few minutes."
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
    if result.timed_out:
        parts.append("Arctic Shift timed out on part of the range, so some posts were skipped; "
                     "fetch again later to fill them in.")
    if result.comment_errors:
        parts.append(f"Comments couldn't be fetched for {plural(result.comment_errors, 'post')}; "
                     "fetch again later to fill them in.")
    if result.stopped:
        parts.append("Stopped early.")
    return " ".join(parts)


def when_text(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%d %b %Y, %H:%M") if ts else "never"


class MainWindow(QMainWindow):
    LOAD_THREAD_FROM = 10_000  # saved posts and comments; a subreddit this big is read on a thread

    def __init__(self, db_path: str):
        super().__init__()
        self.setWindowTitle("ikari")
        self.resize(fit_to_screen(QSize(1320, 860), QGuiApplication.primaryScreen()))
        self.db_path = db_path
        self.store = Store(db_path)
        # Packs the text of data saved before day packs existed, without holding up the window (store.PACKED).
        self._upgrade_stop = threading.Event()
        self.upgrading = threading.Thread(target=Store.upgrade, args=(db_path, self._upgrade_stop), daemon=True)
        self.upgrading.start()
        self.settings = QSettings()
        self.updates = UpdateFlow(self, self.settings)
        self.fetch_job = BackgroundJob(self)
        self.fetch_job.ended.connect(self._on_fetch_ended)
        self.load_job = BackgroundJob(self)  # reads a big saved subreddit
        self.load_job.ended.connect(self._load_wanted)
        self._wanted: tuple | None = None  # (name, score filter) being read on load_job's thread, to show then
        self._stopping = False  # Stop was pressed on the running fetch
        self.current: str | None = None
        self.request: collect.FetchRequest | None = None
        self._live = QTimer(self)
        self._live.setSingleShot(True)
        self._live.setInterval(1000)
        self._live.timeout.connect(self._live_refresh)
        self._range_timer = QTimer(self)
        self._range_timer.setSingleShot(True)
        self._range_timer.setInterval(300)
        self._range_timer.timeout.connect(self._refresh_coverage_range)
        self._drip = QTimer(self)  # releases fetched rows into the tables a few at a time, so each is seen
        self._drip.setInterval(40)
        self._drip.timeout.connect(self._release_rows)
        # The overview follows the rows a running fetch saves, a few times a second: often enough to read as
        # live, rarely enough that each change has time to ease in.
        self._shown_live = QTimer(self)
        self._shown_live.setSingleShot(True)
        self._shown_live.setInterval(250)
        self._shown_live.timeout.connect(self._render_overview)
        # The tables follow the timeline's span once it settles, not at every step of a drag or a zoom.
        self._span_settled = QTimer(self)
        self._span_settled.setSingleShot(True)
        self._span_settled.setInterval(300)
        self._span_settled.timeout.connect(self._span_settle)
        self._span_slices = Slices(self)
        self._queued: dict[str, list] = {"posts": [], "comments": []}
        self._queued_ids: set[str] = set()
        self._since = 0.0  # rows saved since this moment belong to the running fetch
        # How far each table has read the running fetch's rows, so each save reads only what's new.
        self._read_to = {"posts": 0.0, "comments": 0.0}
        self._follow = False  # show the subreddit being fetched, until the user picks another one
        self.posts: list = []  # what the tables show and export writes: the saved rows from the days in the span
        self.comments: list = []
        # Every saved row of the subreddit shown, kept up as a running fetch saves more; the overview counts the
        # days of it in the span.
        self._analysis = {"posts": Analysis(), "comments": Analysis()}
        self._last_fetched: float | None = None  # newest fetched_at among self.posts
        self._saved: dict[str, list] = {"posts": [], "comments": []}  # every saved row of the subreddit shown
        # The span of days every page counts, as the timeline shows it: [lo, hi) in epoch seconds, None for
        # every day; and the timeline's range button it came from, if any.
        self.span: tuple[float, float] | None = None
        self._preset: str | None = "All"
        self._table_days: tuple[int, int] | None = None  # the whole days the tables were filtered to
        self._following = False  # the timelines are being given new days
        self._known: dict[str, set[str]] = {}  # ids already in _saved and _analysis, once a running fetch is counted in
        # The last few subreddits shown, (_saved, _analysis) by (name, score filter), with what the saved-data list
        # said of each then (posts, comments, last fetched): switching back to one needn't read and count it again.
        self._loaded: dict[tuple, tuple] = {}
        self._stats: dict[str, tuple] = {}
        self.setWindowIcon(QIcon(str(theme.ASSETS / "app.png")))
        # A native material (Mica/Acrylic/vibrancy) needs a translucent window from the start,
        # so a change applies on next launch.
        self._native_backdrop = native.prepare_window(self, self.settings.value("ui/backdrop", "gradient", str))
        self._shown = False
        self._counted = Analysis()  # what the Overview and Timeline show
        self.mood = theme.MoodTint(self, self.settings.value("ui/mood_tint", True, bool))
        motion.set_enabled(not self.settings.value("ui/reduce_motion", False, bool))

        self._build_menu()
        backdrop = QWidget()  # transparent: the window paints the backdrop behind menus too
        root = self._root = QHBoxLayout(backdrop)
        root.setContentsMargins(12, 4, 12, 12)
        root.setSpacing(12)
        self.rail = NavRail()
        self.rail.page_chosen.connect(self.show_page)
        self.rail.drawer_toggled.connect(self.set_drawer)
        root.addWidget(self.rail)
        root.addWidget(self._build_drawers())
        root.addWidget(self._build_content(), 1)
        self.setCentralWidget(backdrop)
        # Permanent, so temporary messages (exports, restarts) don't wipe the access-mode note.
        # The status bar only appears while it has something to say (an export, a deletion).
        bar = self.statusBar()
        bar.messageChanged.connect(self._status_changed)
        bar.hide()
        QGuiApplication.styleHints().colorSchemeChanged.connect(lambda _: self._apply_theme())
        self._refresh_datasets(select=self.settings.value("ui/last_dataset", "", str))
        # "ui/tab" held the index of the old Overview / Posts / Comments tabs.
        old_tab = ("overview", "posts", "comments")[min(max(self.settings.value("ui/tab", 0, int), 0), 2)]
        page = self.settings.value("ui/page", old_tab, str)
        self.show_page(page if page in PAGES else "overview")
        if not self.datasets.count():  # nothing saved yet: start where a fetch starts
            self.set_drawer("fetch")
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
        # Says which database is open: one started under another home (sudo, a Flatpak IDE) is a different file.
        file_menu.addSeparator()
        self.import_act = QAction("Import database…", self, triggered=self._import_database)
        file_menu.addAction(self.import_act)
        self.data_folder_act = QAction("Open data folder", self, statusTip=str(self.store.path),
                                       triggered=lambda: QDesktopServices.openUrl(
                                           QUrl.fromLocalFile(str(self.store.path.parent))))
        file_menu.addAction(self.data_folder_act)
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
        self.motion_act = QAction("Reduce motion", self, checkable=True,
                                  checked=self.settings.value("ui/reduce_motion", False, bool))
        self.motion_act.setToolTip("Show new data and other views at once, without fading")
        self.motion_act.toggled.connect(self._set_reduce_motion)
        view.addAction(self.motion_act)
        self.mood_act = QAction("Tint background by sentiment", self, checkable=True, checked=self.mood.enabled)
        self.mood_act.toggled.connect(self._set_mood_tint)
        view.addAction(self.mood_act)
        view.addSeparator()
        self.coverage_act = QAction("Download coverage", self, shortcut=QKeySequence("Ctrl+D"),
                                    triggered=lambda: self.show_page("coverage"))
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

    def _build_drawers(self) -> QWidget:
        """The Fetch and Saved drawers, which open beside the rail. One shows at a time, in the same slot."""
        self.drawer = QStackedWidget()
        self.drawer.setMaximumWidth(DRAWER_WIDTH)
        # Its width doesn't count towards the window's smallest size: where the page beside it would get less
        # than it needs, the drawer floats over the page instead (_place_drawer).
        self.drawer.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        self.drawer.hide()

        fetch, lay = self._drawer_panel("Fetch", "Gather posts and read the mood")
        self.fetch_form = FetchForm(self.settings, self)
        self.fetch_form.submitted.connect(self._start_fetch)
        self.fetch_form.range_moved.connect(self._range_moved)
        lay.addLayout(self.fetch_form.layout)
        self.fetch_form.align_labels()
        self.fetch_btn = mark(QPushButton("Fetch and analyse"), "primary", True)
        self.fetch_btn.setDefault(True)
        self.fetch_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.fetch_btn.clicked.connect(self._start_fetch)
        lay.addWidget(self.fetch_btn)
        lay.addStretch(1)
        self.drawer.addWidget(fetch)

        saved, lay = self._drawer_panel("Saved data", "Kept on this computer between sessions")
        self.delete_btn = mark(mark(QPushButton("Delete…"), "flat", True), "small", True)
        self.delete_btn.setToolTip("Delete the saved posts and comments for the selected subreddit")
        self.delete_btn.clicked.connect(self._delete_dataset)
        self.delete_all_btn = mark(mark(QPushButton("Delete all…"), "flat", True), "small", True)
        self.delete_all_btn.setToolTip("Delete every saved subreddit")
        self.delete_all_btn.clicked.connect(self._delete_all)
        saved_head = QHBoxLayout()
        saved_head.setSpacing(2)
        saved_head.addWidget(section("Subreddits"), 1)
        saved_head.addWidget(self.delete_btn)
        saved_head.addWidget(self.delete_all_btn)
        lay.addLayout(saved_head)
        self.datasets = QListWidget()
        self.datasets.setFrameShape(QFrame.Shape.NoFrame)
        # Fills whatever height is left, down to one row; its default preferred height would count in full.
        self.datasets.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.datasets.setMinimumHeight(42)
        QuietScrollBars(self.datasets)
        self.datasets.currentItemChanged.connect(self._on_dataset_selected)
        lay.addWidget(self.datasets, 1)
        self.storage_info = label("", "muted")
        self.storage_info.setWordWrap(True)
        self.storage_info.setToolTip(f"Saved on this computer, kept between sessions, in {self.store.path}")
        lay.addWidget(self.storage_info)
        self.drawer.addWidget(saved)
        return self.drawer

    @staticmethod
    def _drawer_panel(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
        """A drawer's panel: its title over a body that scrolls on a window too short for it."""
        side, outer = panel("true", margins=1, spacing=0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(19, 16, 19, 16)
        lay.setSpacing(12)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        # The scroll bar sits in the right margin when it shows, so the fields don't shift left.
        bar = scroll.verticalScrollBar()
        bar.rangeChanged.connect(
            lambda _lo, hi: lay.setContentsMargins(19, 16, 19 - (bar.sizeHint().width() if hi > 0 else 0), 16))
        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand.addWidget(label(title, "title"))
        brand.addWidget(label(subtitle, "subtitle"))
        lay.addLayout(brand)
        lay.addSpacing(4)
        return side, lay

    def _build_content(self) -> QWidget:
        # One panel: the header on top, a rule under it, then the page.
        content, lay = panel("true", margins=0, spacing=0)

        self.heading = ElidedLabel("No data yet", "heading")
        self.subheading = ElidedLabel(NO_DATA, "subtitle")
        # What the Overview and Timeline count.
        self.scope = mark(ShrinkingComboBox(), "pill", True)
        self.scope.addItems(SCOPES)
        self.scope.setToolTip("Count posts, comments or both on the Overview and Timeline")
        saved = self.settings.value("ui/overview_scope", SCOPES[0], str)
        self.scope.setCurrentIndex(max(self.scope.findText(saved), 0))
        self.scope.currentTextChanged.connect(self._on_scope_changed)
        # Which posts every page counts by score, and the comments on them. It only hides, never deletes.
        self.score_pick = mark(ShrinkingComboBox(), "pill", True)
        for text, value in MIN_SCORES.items():
            self.score_pick.addItem(text, value)
        self.score_pick.setToolTip("Count only posts scoring at least this much, and the comments on them")
        self.score_pick.setCurrentIndex(max(self.score_pick.findText(self.settings.value("ui/min_score", "", str)), 0))
        self.score_pick.currentIndexChanged.connect(self._on_min_score)
        self.heading.setMinimumWidth(QFontMetrics(theme.display_font(14, 600)).horizontalAdvance("r/subreddit"))

        # The fetch running, or what the last one did, on every page: the drawer it started from closes.
        self.fetch_strip = FetchStrip()
        self.progress, self.progress_msg, self.stop_btn = (
            self.fetch_strip.progress, self.fetch_strip.message, self.fetch_strip.stop_btn)
        self.stop_btn.clicked.connect(self._stop_fetch)

        header = QGridLayout()
        header.setContentsMargins(16, 12, 16, 12)
        header.setHorizontalSpacing(12)
        header.setVerticalSpacing(2)
        header.addWidget(self.heading, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        # The scope shows on the Overview and Timeline only; a row keeps the score filter at the right edge
        # without a hidden column's spacing.
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addWidget(self.scope)
        controls.addWidget(self.score_pick)
        header.addLayout(controls, 0, 1, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.subheading, 1, 0, 1, 2)
        header.addWidget(self.fetch_strip, 2, 0, 1, 2)
        header.setColumnStretch(0, 1)
        lay.addLayout(header)
        lay.addWidget(mark(QFrame(), "role", "divider"))

        self.pages = QStackedWidget()
        self.content = content
        self.overview = OverviewPage()
        timeline = TimelineChart()
        self.timeline_page = CardPage("Mean sentiment per day (UTC)", timeline, timeline.toolbar)
        self.timelines = (self.overview.timeline, timeline)
        for chart in self.timelines:
            chart.note_requested.connect(self._edit_note)
            chart.view_changed.connect(partial(self._on_view_changed, chart))
        self.words_page = CardPage("Words used most", WordCloud())
        self.word_tally = WordTally(self)
        self.word_tally.changed.connect(self._render_words)
        self.posts_tab = TablePage(POST_COLUMNS, stretch_col=2)
        self.comments_tab = TablePage(COMMENT_COLUMNS, stretch_col=2)
        # The post title would otherwise size to its text and squeeze the comment column.
        post_col = self.comments_tab.view.horizontalHeader()
        post_col.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        post_col.resizeSection(4, 180)
        self.coverage = CoveragePage(self)
        self.page_widgets = {"overview": self.overview, "timeline": self.timeline_page, "words": self.words_page,
                             "posts": self.posts_tab, "comments": self.comments_tab, "coverage": self.coverage}
        for key in PAGES:
            self.pages.addWidget(self.page_widgets[key])
        body = QVBoxLayout()
        body.setContentsMargins(16, 16, 16, 16)
        body.addWidget(self.pages, 1)
        lay.addLayout(body, 1)
        return content

    # -- pages and drawers ----------------------------------------------------

    def page(self) -> str:
        return self.rail.page()

    def show_page(self, key: str) -> None:
        if self.pages.currentWidget() is not self.page_widgets[key]:
            motion.crossfade(self.content)  # first: the old page's look
        self.rail.set_page(key)
        self.pages.setCurrentWidget(self.page_widgets[key])
        self.scope.setVisible(key in ("overview", "timeline", "words"))
        self.score_pick.setVisible(key != "coverage")  # Download coverage counts every saved post
        self.settings.setValue("ui/page", key)
        if key == "timeline":  # kept up only while it shows
            self._show_span(self.timeline_page.body)
        elif key == "words":
            self._render_words()

    def set_drawer(self, key: str) -> None:
        """Open the Fetch or Saved drawer, or close the open one with ""."""
        if key == self._drawer_key():
            return
        motion.crossfade(self.centralWidget())
        self.rail.set_drawer(key)
        if key:
            self.drawer.setCurrentIndex(list(DRAWERS).index(key))
        self.drawer.setVisible(bool(key))
        self._place_drawer()
        if key == "fetch":
            self.fetch_form.subreddit.setFocus()

    def _place_drawer(self) -> None:
        """Beside the rail, pushing the page over, where the window is wide enough for both; floating over the
        page's left edge where it isn't (a 1280x800 screen at 125%)."""
        root, central = self._root, self.centralWidget()
        m = root.contentsMargins()
        left = m.left() + self.rail.sizeHint().width() + root.spacing()  # where the drawer starts, from the rail
        room = central.width() - left - root.spacing() - m.right()
        push = room >= DRAWER_WIDTH + self.content.minimumSizeHint().width()
        placed = root.indexOf(self.drawer) >= 0
        if push and not placed:
            root.insertWidget(1, self.drawer)
        elif not push and placed:
            root.removeWidget(self.drawer)
        if not push:
            self.drawer.setGeometry(left, m.top(), DRAWER_WIDTH, central.height() - m.top() - m.bottom())
            self.drawer.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if not self.drawer.isHidden():
            self._place_drawer()

    def _drawer_key(self) -> str:
        return "" if self.drawer.isHidden() else list(DRAWERS)[self.drawer.currentIndex()]

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self._drawer_key():
            self.set_drawer("")
        else:
            super().keyPressEvent(event)

    # -- theme ----------------------------------------------------------------

    def _set_theme(self, key: str) -> None:
        self.settings.setValue("ui/theme", key)
        self._apply_theme()

    def _set_reduce(self, on: bool) -> None:
        self.settings.setValue("ui/reduce_transparency", on)
        self._apply_theme()

    def _set_reduce_motion(self, on: bool) -> None:
        self.settings.setValue("ui/reduce_motion", on)
        motion.set_enabled(not on)

    def _status_changed(self, text: str) -> None:
        """The status bar only shows while it has something to say, and fades in when it appears (not on
        each new message while it's already up)."""
        bar = self.statusBar()
        appearing = bool(text) and not bar.isVisible()
        bar.setVisible(bool(text))
        if appearing:
            motion.fade_in(bar)

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
        motion.crossfade(self, motion.THEME_MS, still_under=True)
        requested = self.settings.value("ui/theme", "system", str)
        native.set_appearance(requested)
        theme.apply(QApplication.instance(), requested, self.settings.value("ui/reduce_transparency", False, bool),
                    self)
        self.update()
        self._style_native_frame()
        for tab in (self.posts_tab, self.comments_tab):  # re-pick swatch and chart colours
            tab.model.restyle()
        self._render_overview()

    # -- control state --------------------------------------------------------

    def _range_moved(self) -> None:
        """The Download coverage page lists Fetch's range, so it follows a change: shortly after it, so
        holding an arrow key on a date doesn't rebuild the page (and ask for counts) per step."""
        if getattr(self, "coverage", None) is not None and self.coverage.isVisible():  # the form moves as it's built
            self._range_timer.start()

    def _refresh_coverage_range(self) -> None:
        self.coverage.refresh_saved()

    def _set_busy(self, busy: bool) -> None:
        # Called after the fetch job starts (busy) or ends (idle); the update helpers read it.
        self.fetch_btn.setEnabled(not busy)
        self.fetch_strip.set_busy(busy)
        self.rail.buttons["fetch"].set_busy(busy)
        if busy:
            self.progress.setRange(0, 0)
            motion.fade_in(self.progress)
        self.fetch_form.set_busy(busy)
        self._update_delete_enabled()  # don't delete rows the fetch thread is writing
        self.coverage.set_busy(busy)

    def _update_delete_enabled(self) -> None:
        idle = not self.fetch_job.busy
        self.delete_btn.setEnabled(idle and bool(self.current))
        self.delete_act.setEnabled(idle and bool(self.current))
        self.delete_all_btn.setEnabled(idle and self.datasets.count() > 0)
        self.delete_all_act.setEnabled(idle and self.datasets.count() > 0)
        self.import_act.setEnabled(idle)

    # -- fetching -------------------------------------------------------------

    def _start_fetch(self) -> None:
        if self.fetch_job.busy:
            return
        field = self.fetch_form.subreddit
        name = normalize_subreddit(field.text())
        if error := subreddit_error(name):
            self.progress_msg.setText(error)
            field.setFocus()
            field.selectAll()
            return
        self._launch(self.fetch_form.request(name), f"Contacting Arctic Shift for r/{name}…")

    def start_request(self, req: collect.FetchRequest, message: str) -> bool:
        """Run a fetch the coverage panel asked for; False if one is already running."""
        if self.fetch_job.busy:
            return False
        self._launch(req, message)
        return True

    def _launch(self, req: collect.FetchRequest, message: str) -> None:
        self.request = req
        self._since = time.time() - 1.0
        self._read_to = {"posts": self._since, "comments": self._since}
        self._flush_rows()
        self._follow = True
        self._open_fetched()  # if it's already saved, show it now; otherwise with its first rows
        worker = FetchWorker(req, self.db_path)
        worker.progress.connect(self._on_progress)
        worker.finished.connect(self._on_finished)
        worker.saved.connect(self._on_saved)
        worker.failed.connect(self._on_failed)
        self.fetch_job.start(worker)
        self._set_busy(True)
        self._stopping = False
        self.progress_msg.setText(message)
        if self._drawer_key() == "fetch":  # the strip under the header follows it from here
            self.set_drawer("")

    def _stop_fetch(self) -> None:
        if self.fetch_job.busy:
            self.fetch_job.stop()
            self._stopping = True
            self.progress_msg.setText("Stopping…")
            self.stop_btn.setEnabled(False)

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if self._stopping:  # what's still finishing ("Waiting 4 s…") would read as Stop being ignored
            msg = "Stopping…"
        self.progress_msg.setText(msg)

    def _on_saved(self) -> None:
        # A row was committed: queue it for the tables right away, and refresh the sidebar list and
        # overview charts at most about once a second so big fetches stay smooth.
        if self._follow and not self._showing_fetch():
            self._open_fetched()
        self._queue_new_rows()
        if not self._live.isActive():
            self._live.start()

    def _open_fetched(self) -> None:
        """Show the subreddit being fetched."""
        self._refresh_datasets(select=self.request.subreddit, reload=False)
        if self._showing_fetch():
            return
        # Nothing of it saved yet shows empty, filling as its first rows arrive.
        item = self.datasets.currentItem()
        saved = item and item.data(Qt.ItemDataRole.UserRole)
        if saved and saved.lower() == self.request.subreddit.lower():
            self._show_dataset(saved)
        else:
            self.datasets.blockSignals(True)
            self.datasets.setCurrentRow(-1)  # it has no row yet
            self.datasets.blockSignals(False)
            self._show_dataset(self.request.subreddit)
        for kind, rows in self._saved.items():
            # What was just loaded needn't be read again; rows sharing the last instant are caught by id.
            self._read_to[kind] = max([self._read_to[kind]] + [r["fetched_at"] for r in rows])

    def _showing_fetch(self) -> bool:
        return bool(self.request and self.current and self.current.lower() == self.request.subreddit.lower())

    def _queue_new_rows(self) -> None:
        if not self.fetch_job.busy or not self._showing_fetch():
            return
        name = self.current
        for kind, read in (("posts", self.store.posts_since), ("comments", self.store.comments_since)):
            rows = read(name, self._read_to[kind])
            if rows:  # rows saved in the same instant as the last one read come back too; the ids catch them
                self._read_to[kind] = rows[-1]["fetched_at"]
                rows = scoring_at_least(rows, self.min_score)
                self._count_new(kind, rows)
            for r in days.keep(rows, self._table_days):
                if r["id"] not in self._queued_ids:
                    self._queued_ids.add(r["id"])
                    self._queued[kind].append(r)
        if any(self._queued.values()) and not self._drip.isActive():  # start() would restart it, and
            self._drip.start()  # comments saved every few ms would then hold every row back

    def _count_new(self, kind: str, rows) -> None:
        """Count rows the running fetch saved into the overview, whatever their day: the timeline shows every
        day, and the span can be moved onto them later. Rows saved before, saved again with new scores,
        aren't counted twice (the finished fetch brings their new scores in)."""
        known = self._known.get(kind)
        if known is None:
            known = self._known[kind] = {r["id"] for r in self._saved[kind]}
        new = [r for r in rows if r["id"] not in known and r["created_utc"] is not None]
        if new:
            known.update(r["id"] for r in new)
            self._saved[kind].extend(new)
            self._analysis[kind].add(new)
            self.word_tally.add(kind, days.keep(new, self._table_days))
            if not self._shown_live.isActive():
                self._shown_live.start()

    def _release_rows(self) -> None:
        """Show the next few queued rows; a long backlog is released faster so it never lags far behind."""
        for kind, tab in (("posts", self.posts_tab), ("comments", self.comments_tab)):
            queue = self._queued[kind]
            batch, self._queued[kind] = queue[:max(1, len(queue) // 25)], queue[max(1, len(queue) // 25):]
            if batch:
                self._add_rows(kind, tab, batch)
        if not any(self._queued.values()):
            self._drip.stop()
        self._update_dataset_labels()

    def _add_rows(self, kind: str, tab: TablePage, rows) -> None:
        before = len(tab.all_rows)
        tab.add_rows(rows)
        new = tab.all_rows[before:]
        if kind == "posts" and new:
            self._last_fetched = max([self._last_fetched or 0.0] + [r["fetched_at"] for r in new])

    def _flush_rows(self) -> None:
        self._drip.stop()
        for kind, tab in (("posts", self.posts_tab), ("comments", self.comments_tab)):
            if self._queued[kind]:
                self._add_rows(kind, tab, self._queued[kind])
            self._queued[kind] = []
        self._queued_ids.clear()

    def _update_dataset_labels(self) -> None:
        self.export_posts.setEnabled(bool(self.posts))
        self.export_comments.setEnabled(bool(self.comments))
        if not self.current:
            self.heading.setText("No data yet")
            self.subheading.setText(NO_DATA)
            return
        self.heading.setText(f"r/{self.current}")
        # How fresh the data is, which days of it the timeline's span counts (on every page, the tables too),
        # and how much they hold.
        span = "" if self._table_days is None else f" · {days.span_text(self._table_days)}"
        self.subheading.setText(f"Last fetched {when_text(self._last_fetched)}{span} · "
                                f"{plural(len(self.posts), 'post')} · {plural(len(self.comments), 'comment')}")

    def _live_refresh(self) -> None:
        # What reads the store again (the saved-data list, Download coverage) once a second; the overview
        # follows the rows as they're counted instead (_count_new).
        if not self.fetch_job.busy or self.request is None:
            return
        if self._showing_fetch():
            self._refresh_datasets(reload=False)
        elif self._follow:
            self._open_fetched()
        else:
            self._refresh_datasets(reload=False)
        self.coverage.refresh_saved()

    def _on_finished(self, result: collect.FetchResult) -> None:
        self._loaded.clear()  # what it saved (scores too) is read again, not shown from memory
        self.progress_msg.setText(finish_text(result, self.request))
        self._live.stop()
        self._flush_rows()
        if self.current and self.current.lower() == result.subreddit.lower():
            self._refresh_datasets(reload=False)
            self._merge_fetched()  # keeps the tables where they are
        elif self._follow and (result.posts or result.updated or result.comments):
            self._refresh_datasets(select=result.subreddit)
        elif not self._follow:  # the user went to look at another subreddit meanwhile: stay there
            self._refresh_datasets(reload=False)

    def _merge_fetched(self) -> None:
        """Bring what the finished fetch saved into the dataset on screen: rows it saved again (new scores)
        and any not dripped in yet. Reading back only these, not every saved row, keeps a big subreddit
        from freezing the window for seconds as the fetch ends."""
        if self._span_slices.running():  # the tables are mid-way to a new span: finish it with the rows as they are
            self._apply_span()
        dropped: set[str] = set()
        for kind, read, tab in (("posts", self.store.posts_since, self.posts_tab),
                                ("comments", self.store.comments_since, self.comments_tab)):
            rows = read(self.current, self._since)
            # A post rescored below the score filter leaves, with its comments (posts come first).
            dropped |= {r["id"] for r in rows} - {r["id"] for r in scoring_at_least(rows, self.min_score)}
            owner = "id" if kind == "posts" else "post_id"
            rows = [r for r in rows if r["id"] not in dropped and r[owner] not in dropped]
            saved = self._saved[kind] = [r for r in self._saved[kind] if r["id"] not in dropped and r[owner] not in dropped]
            where = {r["id"]: i for i, r in enumerate(saved)}
            for r in rows:
                if r["id"] in where:
                    saved[where[r["id"]]] = r
                else:
                    saved.append(r)
            self._analysis[kind] = Analysis(saved)  # a rescored row changes the totals
            self.word_tally.add(kind, days.keep(rows, self._table_days))  # by id: rows saved again replace their words
            tab.update_rows(days.keep(rows, self._table_days))
        self._known = {}
        self._last_fetched = max((r["fetched_at"] for r in self._saved["posts"]), default=None)
        if dropped:  # the tables only add and replace rows
            self._apply_span(reload=True)
        self._update_dataset_labels()
        self._render_overview()

    def _on_failed(self, message: str) -> None:
        self._loaded.clear()
        self._live.stop()
        self._flush_rows()
        # Show anything stored before the error, unless the user went to look at another subreddit.
        self._refresh_datasets(select=self.request.subreddit if self._follow else None)
        kept = self.current and self.current.lower() == self.request.subreddit.lower()
        self.progress_msg.setText("Fetch failed. What was fetched before the error is saved." if kept
                                  else "Fetch failed.")
        QMessageBox.warning(self, "Fetch failed", message)

    def _on_fetch_ended(self) -> None:
        self._set_busy(False)
        if self.current and self.current.lower() not in self._stats:  # it saved nothing: back to a saved one
            self._refresh_datasets(select=self.settings.value("ui/last_dataset", "", str))

    # -- datasets -------------------------------------------------------------

    def _refresh_datasets(self, select: str | None = None, reload: bool = True) -> None:
        """Reload the saved-data list, selecting select, else the current dataset, else the newest.
        With reload=False only the list and storage line change, not the dataset on screen."""
        sets = self.store.datasets()
        self._stats = {d["name"].lower(): (d["posts"], d["comments"], d["last_fetched"]) for d in sets}
        names = [d["name"] for d in sets]
        if names != [self.datasets.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.datasets.count())]:
            motion.crossfade(self.datasets.viewport())
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
        if row is not None and not (self.fetch_job.busy and self._showing_fetch() and self.current.lower() not in lowered):
            self.datasets.setCurrentRow(row)  # (a fetch's subreddit with nothing saved yet has no row)
        else:
            self.datasets.setCurrentRow(-1)
        self.datasets.blockSignals(False)
        if reload:
            self._on_dataset_selected(self.datasets.currentItem())  # reload even when the selection is unchanged
        posts, comments = sum(d["posts"] for d in sets), sum(d["comments"] for d in sets)
        self.storage_info.setText(
            f"{plural(posts, 'post')}, {plural(comments, 'comment')} · {human_size(self.store.size_bytes())}" if sets
            else "Nothing saved yet. Fetched data is kept on this computer between sessions.")
        self._update_delete_enabled()

    def _on_dataset_selected(self, item: QListWidgetItem | None) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if self.fetch_job.busy and self.request and name.lower() != self.request.subreddit.lower():
            self._follow = False  # the user chose to look at something else while it runs
        self._show_dataset(name)
        if name:
            self.settings.setValue("ui/last_dataset", name)

    def _show_dataset(self, name: str, wait: bool = False) -> None:
        """Show a saved subreddit. One shown lately is shown from memory; a big one not in memory is read on a
        thread (unless ``wait``, or the window isn't up yet) and shown once it's in, the window carrying on."""
        key, stats = (name.lower(), self.min_score), self._stats.get(name.lower())
        fetching = bool(self.fetch_job.busy and self.request and name.lower() == self.request.subreddit.lower())
        loaded = self._loaded.pop(key, None)
        if loaded is not None and (loaded[0] != stats or fetching):
            loaded = None
        if (loaded is None and not wait and not fetching and self.isVisible() and stats
                and stats[0] + stats[1] >= self.LOAD_THREAD_FROM):  # (before the window shows, nothing waits)
            self._wanted = key
            self.subheading.setText(f"Opening r/{name}…")
            self._load_wanted()
            return
        self._wanted = None
        # Rows still queued from a running fetch belong to whatever was shown before; the tables are
        # about to be reloaded from the store, which has them anyway.
        self._drip.stop()
        self._queued = {"posts": [], "comments": []}
        switching = (name or None) != self.current
        if switching:  # the old subreddit fades out over the new one
            motion.crossfade(self.content)
        self.current = name or None
        if loaded is None:
            with self.store.snapshot():
                saved = {"posts": scoring_at_least(self.store.posts(name) if name else [], self.min_score),
                         "comments": scoring_at_least(self.store.comments(name) if name else [], self.min_score)}
            loaded = (stats, saved, {k: Analysis(rows) for k, rows in saved.items()})
        self._loaded[key] = loaded  # the newest last; a running fetch's rows go into these as they're counted
        while len(self._loaded) > 4:
            del self._loaded[next(iter(self._loaded))]
        _, self._saved, self._analysis = loaded
        self._known = {}
        self._last_fetched = max((r["fetched_at"] for r in self._saved["posts"]), default=None)
        # The span remembered for it, shown on the timelines once they have its days.
        self.span, self._preset = days.load_range(self.settings, name)
        self._render_overview(restore=True)
        if self.isVisible() and sum(map(len, self._saved.values())) >= self.LOAD_THREAD_FROM:
            self._span_settled.stop()  # a big one's tables and words fill in over the next few frames
            self._span_slices.start(self._span_steps(reload=True), lambda: None)
        else:
            self._apply_span(reload=True)
        self._show_notes()
        self._update_delete_enabled()
        self.coverage.set_subreddit(self.current)

    def _load_wanted(self) -> None:
        """Read the subreddit last asked for on load_job's thread, once the one before is in."""
        if self._wanted is None or self.load_job.busy:
            return
        name, min_score = key = self._wanted
        worker = LoadWorker(self.db_path, name, min_score, (key, self._stats.get(name)))
        worker.finished.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        self.load_job.start(worker)

    def _on_loaded(self, read: tuple) -> None:
        (key, stats), saved, analysis = read
        self._loaded[key] = (stats, saved, analysis)
        if self._wanted == key:
            self._show_dataset(self._name_of(key[0]))

    def _on_load_failed(self, message: str) -> None:
        if self._wanted is not None:  # read it here instead, where an error shows as it always has
            self._show_dataset(self._name_of(self._wanted[0]), wait=True)

    def _name_of(self, lowered: str) -> str:
        """A saved subreddit's name as the list writes it."""
        for i in range(self.datasets.count()):
            name = self.datasets.item(i).data(Qt.ItemDataRole.UserRole)
            if name.lower() == lowered:
                return name
        return lowered

    def _on_view_changed(self, chart: TimelineChart) -> None:
        """A timeline was zoomed, dragged or set to a range button: every page now counts its span."""
        for other in self._kept_up():
            if other is not chart:
                self._show_span(other, chart)
        if (chart.view, chart.preset) == (self.span, self._preset):
            return
        self.span, self._preset = chart.view, chart.preset
        if self.current and not self._following:  # a fetch or the scope moved it, not the user
            days.save_range(self.settings, self.current, self.span, self._preset)
        self._render_span(moving=True)
        self._span_settled.start()

    def _show_span(self, chart: TimelineChart, like: TimelineChart | None = None) -> None:
        """Show the counted days on ``chart`` and the span ``like`` shows, else the span every page counts,
        without telling the window of it again."""
        chart.blockSignals(True)
        chart.set_points(self._counted.daily_means)
        chart.show_span(*((like.view, like.preset) if like else (self.span, self._preset)))
        chart.blockSignals(False)

    def _span_settle(self) -> None:
        """The span stopped moving: what follows it at its own pace catches up, a step a frame."""
        self._render_span()
        self._span_settled.stop()
        # Steps left over from a span before this one: the tables may still show it, so go through them all.
        self._span_slices.start(self._span_steps(reload=self._span_slices.running()), lambda: None)

    def _apply_span(self, reload: bool = False) -> None:
        """Point the tables (and so export) at the saved rows from the days in the span, if those changed."""
        self._span_settled.stop()
        reload = reload or self._span_slices.running()
        self._span_slices.stop()
        for _ in self._span_steps(reload):
            pass

    def _span_steps(self, reload: bool = False):
        """_apply_span's work, yielding between the steps that take a while. Each table takes its rows from
        _saved in its own step, so rows a running fetch adds in between are in them."""
        whole = days.whole(self.span)
        if whole == self._table_days and not reload:
            return
        self._table_days = whole
        # Rows still queued are in _saved already, so the tables have them once reloaded.
        self._drip.stop()
        self._queued = {"posts": [], "comments": []}
        self.posts = days.keep(self._saved["posts"], whole)
        self.posts_tab.load(self.posts)
        yield
        self.comments = days.keep(self._saved["comments"], whole)
        self.comments_tab.load(self.comments)
        self._update_dataset_labels()
        yield
        self.word_tally.count(self.posts, self.comments, fresh=reload)

    @property
    def min_score(self) -> int | None:
        return self.score_pick.currentData()

    def _on_min_score(self) -> None:
        self.settings.setValue("ui/min_score", self.score_pick.currentText())
        if not self.current:
            return
        motion.crossfade(self.content)
        self._flush_rows()
        self._show_dataset(self.current)
        if self.fetch_job.busy and self._showing_fetch():  # read the running fetch's rows from here on
            self._read_to = {k: max([self._since] + [r["fetched_at"] for r in self._saved[k]]) for k in self._saved}

    def _on_scope_changed(self, text: str) -> None:
        self.settings.setValue("ui/overview_scope", text)
        motion.crossfade(self.pages)
        self._render_overview()
        self._render_words()

    def _render_overview(self, restore: bool = False) -> None:
        """Show every saved day on the timelines, and the span's days on the tiles and distribution. With
        restore, the timelines show the span the window holds (another subreddit's, as remembered)."""
        scope = self.scope.currentText()
        self._counted = Analysis.combined(
            *[self._analysis[k] for k, skip in (("posts", "Comments only"), ("comments", "Posts only")) if scope != skip])
        for chart in self._kept_up():
            if restore:
                self._show_span(chart)
            else:  # a fetch adding days can move a span that ends on the latest day: _on_view_changed follows it
                self._following = True
                try:
                    chart.set_points(self._counted.daily_means)
                finally:
                    self._following = False
        if restore:
            self.span, self._preset = self.overview.timeline.view, self.overview.timeline.preset
        self._render_span(ease=restore)

    def _kept_up(self) -> list[TimelineChart]:
        """The Overview's timeline, and the Timeline page's while it shows."""
        return [c for c in self.timelines if c is self.overview.timeline or c.isVisible()]

    def _render_span(self, moving: bool = False, ease: bool = False) -> None:
        """The tiles and distribution for the span's days. The background's tint (eased to another subreddit's)
        and the word clouds follow too, unless the span is being moved: they catch up once it settles."""
        a = self._counted.between(days.whole(self.span))
        self.overview.show_analysis(a)
        if not moving:
            self.mood.set_counts(a.counts["positive"], a.counts["negative"], a.count, ease)
            self._render_words()

    def _render_words(self) -> None:
        """The word clouds of the span's items the scope counts: 50 words in the Overview's card, 150 on the
        page. They follow the span once it settles, as the tables do."""
        scope = self.scope.currentText()
        kinds = [k for k, skip in zip(KINDS, ("Comments only", "Posts only")) if scope != skip]
        busy = not self.word_tally.ready
        words = [] if busy else self.word_tally.top(kinds, 150)
        self.overview.words.set_words(words[:50], busy)
        if self.words_page.isVisible():
            self.words_page.body.set_words(words, busy)

    def _show_notes(self) -> None:
        notes = self.store.notes(self.current) if self.current else {}
        for chart in self.timelines:
            chart.set_notes(notes)

    def _edit_note(self, day: int) -> None:
        """Add, change or remove the note on a day the timeline's dot was clicked on."""
        if not self.current:
            return
        dialog = NoteDialog(day, *self.store.notes(self.current).get(day, ("", "")), parent=self)
        dialog.chosen.connect(partial(self._save_note, self.current, day))
        dialog.open()

    def _save_note(self, subreddit: str, day: int, tag: str, text: str) -> None:
        self.store.save_note(subreddit, day, tag, text)
        self._show_notes()

    def _delete_dataset(self) -> None:
        if not self.current or self.fetch_job.busy:
            return
        if QMessageBox.question(
                self, "Delete saved data",
                f"Delete the {len(self._saved['posts']):,} posts and {len(self._saved['comments']):,} comments saved for "
                f"r/{self.current}{', and its timeline notes' if self.store.notes(self.current) else ''}? "
                "This can't be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_subreddit(self.current)
        self._loaded.clear()
        for key in days.settings_keys(self.current):
            self.settings.remove(key)
        self.statusBar().showMessage(f"Deleted the saved data for r/{self.current}.", 8000)
        self.current = None
        self._refresh_datasets()

    def _delete_all(self) -> None:
        if self.fetch_job.busy or not self.datasets.count():
            return
        if QMessageBox.question(
                self, "Delete all saved data",
                f"Delete everything saved for all {self.datasets.count()} subreddits? "
                "This can't be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_all()
        self._loaded.clear()
        self.settings.remove("ui/last_dataset")
        self.settings.remove(days.SETTINGS_GROUP)
        self.settings.remove(days.RANGE_GROUP)
        self.statusBar().showMessage("Deleted all saved data.", 8000)
        self.current = None
        self._refresh_datasets()

    def _export(self, kind: str) -> None:
        if not self.current:
            return
        self._apply_span()  # the span in view now, not as it was before the last zoom settled
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind}", f"{self.current}_{kind}.csv", "CSV files (*.csv)")
        if path:
            try:
                chosen = None if self._table_days is None else {
                    d for d in range(*self._table_days, days.DAY)}
                n = self.store.export_csv(self.current, kind, path, days=chosen, min_score=self.min_score)
            except OSError as e:  # on Windows, usually the file is still open in Excel
                QMessageBox.warning(self, "Export failed",
                                    f"Couldn't save {path}.\n\n{e.strerror or e}\n\n"
                                    "If the file is open in another program, close it and try again.")
                return
            on = "" if chosen is None else (f" from {days.short_date(self._table_days[0], True)} to "
                                            f"{days.short_date(self._table_days[1] - days.DAY, True)}")
            if self.min_score is not None:
                on += f" scoring {self.min_score:,}+" if kind == "posts" else f" on posts scoring {self.min_score:,}+"
            self.statusBar().showMessage(f"Exported {n:,} {kind}{on} to {path}", 8000)

    def _import_database(self) -> None:
        """Merge another ikari database (one saved under another account or computer) into this one."""
        if self.fetch_job.busy:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import database", str(self.store.path.parent),
                                              "ikari databases (*.db);;All files (*)")
        if not path:
            return
        try:
            posts, comments = self.store.import_from(path)
            self._loaded.clear()
        except (sqlite3.DatabaseError, OSError, ValueError) as e:
            QMessageBox.warning(self, "Import failed", f"Couldn't import {path}.\n\n{e}")
            return
        self.statusBar().showMessage(
            f"Imported {plural(posts, 'new post')} and {plural(comments, 'new comment')} from {path}", 8000)
        self._refresh_datasets()
        self.coverage.refresh_saved()

    def _show_about(self) -> None:
        QMessageBox.about(self, "About ikari", (
            f"<h3>ikari {__version__}</h3>"
            "<p>Gather subreddit posts and comments and score their sentiment with a model that runs "
            "on this computer.</p>"
            "<p>Font: Inter, under the SIL Open Font License 1.1.</p>"))

    def paintEvent(self, event) -> None:
        paint_backdrop(self, over_native=self._native_backdrop is not None, mood=self.mood.state)

    def closeEvent(self, event) -> None:
        self.fetch_form.save()
        self.coverage.stop_count()
        self.settings.sync()  # app.main may end the process without Qt's usual clean-up
        if self.fetch_job.busy:
            # A fetch ending after this must not touch the closed store, so nothing it sends is handled.
            thread, worker = self.fetch_job.qthread, self.fetch_job.worker
            worker.stop()
            for sig in (worker.progress, worker.finished, worker.failed, worker.saved, thread.finished):
                sig.disconnect()
            thread.quit()
            thread.wait(3000)
            # Signals it sent just before are already queued, and disconnecting doesn't take them back.
            QCoreApplication.removePostedEvents(self, QEvent.Type.MetaCall)
        self._live.stop()  # both read the store
        self._drip.stop()
        self._shown_live.stop()
        self._span_settled.stop()
        self._span_slices.stop()
        self._wanted = None
        if self.load_job.busy:  # what it read is let go; its thread is waited for, as it uses the file
            self.load_job.worker.finished.disconnect()
            self.load_job.worker.failed.disconnect()
            self.load_job.qthread.quit()
            self.load_job.qthread.wait(3000)
        self._upgrade_stop.set()  # it stops after the day it's packing
        self.upgrading.join()
        self.store.close()
        super().closeEvent(event)

    def running_threads(self) -> list[QThread]:
        """Fetch and count threads still running, such as one stuck on a slow request after the window
        closed."""
        return [j.qthread for j in (self.fetch_job, self.coverage.count_job, self.load_job) if j.running()]
