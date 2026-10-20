from __future__ import annotations

import sqlite3
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
from subsentiment.gui.coverage import CoverageWindow
from subsentiment.gui.fetch_form import FetchForm
from subsentiment.gui.charts import NoteDialog, TimelineChart
from subsentiment.gui.overview import CardPage, OverviewPage
from subsentiment.gui.words import KINDS, WordCloud, WordTally
from subsentiment.gui.rail import DRAWERS, PAGES, NavRail, Segmented
from subsentiment.gui.tables import COMMENT_COLUMNS, POST_COLUMNS, TablePage
from subsentiment.gui.theme import mark, paint_backdrop
from subsentiment.gui.update_flow import UpdateFlow
from subsentiment.gui.widgets import (
    ElidedLabel, QuietScrollBars, ShrinkingComboBox, StatusLabel, fit_to_screen, label, panel, plural, section,
)
from subsentiment.gui.worker import BackgroundJob, FetchWorker
from subsentiment.reddit import normalize_subreddit, subreddit_error
from subsentiment.store import Store


SCOPES = ["Posts and comments", "Posts only", "Comments only"]
# The header's range presets: key -> (label, newest saved days kept; None for every day).
PRESETS = {"week": ("1W", 7), "month": ("1M", 30), "quarter": ("3M", 91), "all": ("All", None)}
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
    def __init__(self, db_path: str):
        super().__init__()
        self.setWindowTitle("ikari")
        self.resize(fit_to_screen(QSize(1320, 860), QGuiApplication.primaryScreen()))
        self.db_path = db_path
        self.store = Store(db_path)
        self.settings = QSettings()
        self.updates = UpdateFlow(self, self.settings)
        self.fetch_job = BackgroundJob(self)
        self.fetch_job.ended.connect(self._on_fetch_ended)
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
        # The overview and day picker follow the rows the drip releases, a few times a second: often enough to
        # read as live, rarely enough that each change has time to ease in.
        self._shown_live = QTimer(self)
        self._shown_live.setSingleShot(True)
        self._shown_live.setInterval(250)
        self._shown_live.timeout.connect(self._show_released)
        self._queued: dict[str, list] = {"posts": [], "comments": []}
        self._queued_ids: set[str] = set()
        self._since = 0.0  # rows saved since this moment belong to the running fetch
        # How far each table has read the running fetch's rows, so each save reads only what's new.
        self._read_to = {"posts": 0.0, "comments": 0.0}
        self._follow = False  # show the subreddit being fetched, until the user picks another one
        self.posts: list = []  # what the analysis counts: the saved rows from the chosen days
        self.comments: list = []
        self._analysis = {"posts": Analysis(), "comments": Analysis()}  # what the overview counts, kept up as rows arrive
        self._last_fetched: float | None = None  # newest fetched_at among self.posts
        self._saved: dict[str, list] = {"posts": [], "comments": []}  # every saved row of the subreddit shown
        self._day_counts: dict[int, tuple[int, int]] = {}
        self.chosen_days: set[int] | None = None  # None: every day
        self._known: dict[str, set[str]] = {}  # ids already in _day_counts, once a running fetch is counted in
        self._days_changed = False  # _day_counts has days the day picker hasn't been given yet
        self.coverage = None  # the Download coverage window, once opened
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
        self.coverage_btn = QPushButton("Download coverage…")
        self.coverage_btn.setToolTip("How much of each day's posts, and each post's comments, is saved")
        self.coverage_btn.clicked.connect(self._show_coverage)
        lay.addWidget(self.coverage_btn)
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
        # Which saved days every page counts (both tables and CSV export too). Picking never fetches.
        self.days_pick = mark(days.DaysCombo(), "pill", True)
        self.days_pick.chosen_changed.connect(self._on_days_chosen)
        # Shortcuts to the saved days: the newest week, month or three months of them, or every day.
        self.presets = Segmented({key: text for key, (text, _) in PRESETS.items()})
        self.presets.setToolTip("Show only the newest saved days")
        self.presets.chosen.connect(self._on_preset)
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
        header.addWidget(self.scope, 0, 1, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.days_pick, 0, 2, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.presets, 0, 3, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.subheading, 1, 0, 1, 4)
        header.addWidget(self.fetch_strip, 2, 0, 1, 4)
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
        self.words_page = CardPage("Words used most", WordCloud())
        self.word_tally = WordTally(self)
        self.word_tally.changed.connect(self._render_words)
        self.posts_tab = TablePage(POST_COLUMNS, stretch_col=2)
        self.comments_tab = TablePage(COMMENT_COLUMNS, stretch_col=2)
        # The post title would otherwise size to its text and squeeze the comment column.
        post_col = self.comments_tab.view.horizontalHeader()
        post_col.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        post_col.resizeSection(4, 180)
        self.page_widgets = {"overview": self.overview, "timeline": self.timeline_page, "words": self.words_page,
                             "posts": self.posts_tab, "comments": self.comments_tab}
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
        self.settings.setValue("ui/page", key)
        if key == "timeline":  # kept up only while it shows
            self.timeline_page.body.set_points(self._counted.daily_means)
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
        motion.crossfade(self, motion.THEME_MS)
        requested = self.settings.value("ui/theme", "system", str)
        native.set_appearance(requested)
        theme.apply(QApplication.instance(), requested, self.settings.value("ui/reduce_transparency", False, bool))
        self.update()
        self._style_native_frame()
        for tab in (self.posts_tab, self.comments_tab):  # re-pick swatch and chart colours
            tab.model.restyle()
        self._render_overview()

    # -- control state --------------------------------------------------------

    def _range_moved(self) -> None:
        """The Download coverage window lists the sidebar's range, so it follows a change: shortly after
        it, so holding an arrow key on a date doesn't rebuild the window (and ask for counts) per step."""
        if getattr(self, "coverage", None) is not None and self.coverage.isVisible():
            self._range_timer.start()

    def _refresh_coverage_range(self) -> None:
        if self.coverage is not None and self.coverage.isVisible():
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
        if self.coverage is not None:
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
            if self.coverage is not None:
                self.coverage.set_progress(0, 0, "Stopping…")
                self.coverage.stop_btn.setEnabled(False)
            self.stop_btn.setEnabled(False)

    def _on_progress(self, done: int, total: int, msg: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if self._stopping:  # what's still finishing ("Waiting 4 s…") would read as Stop being ignored
            msg = "Stopping…"
        self.progress_msg.setText(msg)
        if self.coverage is not None:
            self.coverage.set_progress(done, total, msg)

    def _on_saved(self) -> None:
        # A row was committed: queue it for the tables right away, and refresh the sidebar list and
        # overview charts at most about once a second so big fetches stay smooth.
        if self._follow and not self._showing_fetch():
            self._open_fetched()
        self._queue_new_rows()
        if not self._live.isActive():
            self._live.start()

    def _open_fetched(self) -> None:
        """Show the subreddit being fetched, if anything of it is saved yet."""
        self._refresh_datasets(select=self.request.subreddit, reload=False)
        item = self.datasets.currentItem()
        name = item.data(Qt.ItemDataRole.UserRole) if item else None
        if not name or name.lower() != self.request.subreddit.lower() or self._showing_fetch():
            return
        self._show_dataset(name)
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
                self._count_days(kind, rows)
            for r in days.keep(rows, self.chosen_days):
                if r["id"] not in self._queued_ids:
                    self._queued_ids.add(r["id"])
                    self._queued[kind].append(r)
        if any(self._queued.values()) and not self._drip.isActive():  # start() would restart it, and
            self._drip.start()  # comments saved every few ms would then hold every row back
        if self._days_changed and not self._shown_live.isActive():  # rows on days not chosen still count there
            self._shown_live.start()

    def _count_days(self, kind: str, rows) -> None:
        """Count rows the running fetch saved into the day picker's days, so the picker needn't count every
        saved row again each second (70 ms for 100,000 comments). Rows saved before, saved again with new
        scores, aren't counted twice."""
        known = self._known.get(kind)
        if known is None:
            known = self._known[kind] = {r["id"] for r in self._saved[kind]}
        slot = 0 if kind == "posts" else 1
        for r in rows:
            if r["id"] not in known and r["created_utc"] is not None:
                known.add(r["id"])
                day = days.day_of(r["created_utc"])
                counts = list(self._day_counts.get(day, (0, 0)))
                counts[slot] += 1
                self._day_counts[day] = tuple(counts)
                self._days_changed = True

    def _release_rows(self) -> None:
        """Show the next few queued rows; a long backlog is released faster so it never lags far behind."""
        for kind, tab in (("posts", self.posts_tab), ("comments", self.comments_tab)):
            queue = self._queued[kind]
            batch, self._queued[kind] = queue[:max(1, len(queue) // 25)], queue[max(1, len(queue) // 25):]
            if batch:
                self._add_rows(kind, tab, batch)
                if not self._shown_live.isActive():
                    self._shown_live.start()
        if not any(self._queued.values()):
            self._drip.stop()
        self._update_dataset_labels()

    def _show_released(self) -> None:
        if self._days_changed:
            self._days_changed = False
            self.days_pick.set_days(self._day_counts, self.chosen_days)
        self._render_overview()

    def _add_rows(self, kind: str, tab: TablePage, rows) -> None:
        before = len(tab.all_rows)
        tab.add_rows(rows)
        new = tab.all_rows[before:]
        self._analysis[kind].add(new)
        self.word_tally.add(kind, new)
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
        # The day picker says which days; this line says how fresh they are and how much they hold.
        self.subheading.setText(f"Last fetched {when_text(self._last_fetched)} · {plural(len(self.posts), 'post')}"
                                f" · {plural(len(self.comments), 'comment')}")

    def _live_refresh(self) -> None:
        # What reads the store again (the saved-data list, Download coverage) once a second; the overview and
        # day picker follow the drip instead (_show_released).
        if not self.fetch_job.busy or self.request is None:
            return
        if self._showing_fetch():
            self._refresh_datasets(reload=False)
        elif self._follow:
            self._open_fetched()
        else:
            self._refresh_datasets(reload=False)
        if self.coverage is not None:
            self.coverage.refresh_saved()

    def _on_finished(self, result: collect.FetchResult) -> None:
        self.progress_msg.setText(finish_text(result, self.request))
        if self.coverage is not None:
            self.coverage.set_progress(0, 0, self.progress_msg.text())
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
        for kind, read, tab in (("posts", self.store.posts_since, self.posts_tab),
                                ("comments", self.store.comments_since, self.comments_tab)):
            rows = read(self.current, self._since)
            saved = self._saved[kind]
            where = {r["id"]: i for i, r in enumerate(saved)}
            for r in rows:
                if r["id"] in where:
                    saved[where[r["id"]]] = r
                else:
                    saved.append(r)
            before = len(tab.all_rows)
            self.word_tally.add(kind, days.keep(rows, self.chosen_days))  # by id: rows saved again replace their words
            if tab.update_rows(days.keep(rows, self.chosen_days)):
                self._analysis[kind] = Analysis(tab.all_rows)  # a rescored row changes the totals
            else:
                self._analysis[kind].add(tab.all_rows[before:])
        self._last_fetched = max((r["fetched_at"] for r in self._saved["posts"]), default=None)
        self._refresh_days()
        self._update_dataset_labels()
        self._render_overview()

    def _on_failed(self, message: str) -> None:
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
        if self.coverage is not None:
            self.coverage.refresh_saved()

    # -- datasets -------------------------------------------------------------

    def _refresh_datasets(self, select: str | None = None, reload: bool = True) -> None:
        """Reload the saved-data list, selecting select, else the current dataset, else the newest.
        With reload=False only the list and storage line change, not the dataset on screen."""
        sets = self.store.datasets()
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
        if row is not None:
            self.datasets.setCurrentRow(row)
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

    def _show_dataset(self, name: str) -> None:
        """Show a saved subreddit."""
        # Rows still queued from a running fetch belong to whatever was shown before; the tables are
        # about to be reloaded from the store, which has them anyway.
        self._drip.stop()
        self._queued = {"posts": [], "comments": []}
        switching = (name or None) != self.current
        if switching:  # the old subreddit fades out over the new one
            motion.crossfade(self.content)
            for chart in self.timelines:
                chart.show_every_day()
        self.current = name or None
        self.chosen_days = days.load_choice(self.settings, name) if name else None
        # One snapshot, so a row a running fetch saves meanwhile isn't in the day counts without being in the
        # rows too: it comes in with the fetch's next rows and would be counted twice.
        with self.store.snapshot():
            self._saved = {"posts": self.store.posts(name) if name else [],
                           "comments": self.store.comments(name) if name else []}
            self._refresh_days()
        self._apply_days()
        self._show_notes()
        self._update_delete_enabled()
        self.coverage_act.setEnabled(bool(self.current))
        self.coverage_btn.setEnabled(bool(self.current))
        if self.coverage is not None:
            self.coverage.set_subreddit(self.current)

    def _refresh_days(self) -> None:
        """Re-read which days have saved data, for the day picker."""
        self._day_counts = self.store.day_counts(self.current) if self.current else {}
        self._known = {}
        self._days_changed = False
        self.days_pick.set_days(self._day_counts, self.chosen_days)
        self._mark_preset()

    def _apply_days(self) -> None:
        """Point every view at the saved rows from the chosen days."""
        self.posts = days.keep(self._saved["posts"], self.chosen_days)
        self.comments = days.keep(self._saved["comments"], self.chosen_days)
        self.posts_tab.load(self.posts)
        self.comments_tab.load(self.comments)
        self._analysis = {"posts": Analysis(self.posts), "comments": Analysis(self.comments)}
        self.word_tally.count(self.posts, self.comments)
        self._last_fetched = max((r["fetched_at"] for r in self._saved["posts"]), default=None)
        self._update_dataset_labels()
        self._render_overview()

    def _on_days_chosen(self, chosen: set[int] | None) -> None:
        if not self.current:
            return
        chosen = days.save_choice(self.settings, self.current, chosen, self._day_counts)
        if chosen != self.chosen_days:
            motion.crossfade(self.content)
            for chart in self.timelines:  # other days: from all of them
                chart.show_every_day()
        self.chosen_days = chosen
        self._mark_preset()
        if self.fetch_job.busy and self._showing_fetch():
            # A running fetch has added rows since the subreddit was loaded: read them all again.
            self._flush_rows()
            self._show_dataset(self.current)
            self._read_to = {k: max([self._since] + [r["fetched_at"] for r in self._saved[k]])
                             for k in ("posts", "comments")}
        else:
            self._apply_days()

    def _preset_days(self, key: str) -> set[int] | None:
        """The saved days a preset keeps: those within its span of the newest saved day."""
        span = PRESETS[key][1]
        if span is None or not self._day_counts:
            return None
        newest = max(self._day_counts)
        return {d for d in self._day_counts if d > newest - span * 86400}

    def _on_preset(self, key: str) -> None:
        if not self.current:
            self.presets.set_current("")
            return
        chosen = self._preset_days(key)
        self.days_pick.set_days(self._day_counts, chosen)
        self._on_days_chosen(chosen)
        self.presets.set_current(key)  # kept even when it covers every saved day, as All would

    def _mark_preset(self) -> None:
        """Mark the preset the chosen days match, if any (the one already marked first, when two match)."""
        def matches(key):
            got = self._preset_days(key)
            return (None if got is not None and set(self._day_counts) <= got else got) == self.chosen_days
        keys = [self.presets.current(), "all", *PRESETS] if self.presets.current() else ["all", *PRESETS]
        self.presets.set_current(next((k for k in keys if self.current and matches(k)), ""))

    def _on_scope_changed(self, text: str) -> None:
        self.settings.setValue("ui/overview_scope", text)
        motion.crossfade(self.pages)
        self._render_overview()

    def _render_overview(self) -> None:
        scope = self.scope.currentText()
        a = Analysis.combined(*[self._analysis[k] for k, skip in (("posts", "Comments only"), ("comments", "Posts only"))
                                if scope != skip])
        self._counted = a
        self.overview.show_analysis(a)
        if self.timeline_page.isVisible():
            self.timeline_page.body.set_points(a.daily_means)
        self.mood.set_counts(a.counts["positive"], a.counts["negative"], a.count)
        self._render_words()

    def _render_words(self) -> None:
        """The word clouds, for the items the scope counts: 50 words in the Overview's card, 150 on the page."""
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
        self.settings.remove(days.settings_key(self.current))
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
        self.settings.remove("ui/last_dataset")
        self.settings.remove(days.SETTINGS_GROUP)
        self.statusBar().showMessage("Deleted all saved data.", 8000)
        self.current = None
        self._refresh_datasets()

    def _export(self, kind: str) -> None:
        if not self.current:
            return
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind}", f"{self.current}_{kind}.csv", "CSV files (*.csv)")
        if path:
            try:
                n = self.store.export_csv(self.current, kind, path, days=self.chosen_days)
            except OSError as e:  # on Windows, usually the file is still open in Excel
                QMessageBox.warning(self, "Export failed",
                                    f"Couldn't save {path}.\n\n{e.strerror or e}\n\n"
                                    "If the file is open in another program, close it and try again.")
                return
            n_days = len(self.chosen_days & set(self._day_counts)) if self.chosen_days is not None else 0
            on = "" if self.chosen_days is None else f" from {plural(n_days, 'chosen day')}"
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
        except (sqlite3.DatabaseError, OSError, ValueError) as e:
            QMessageBox.warning(self, "Import failed", f"Couldn't import {path}.\n\n{e}")
            return
        self.statusBar().showMessage(
            f"Imported {plural(posts, 'new post')} and {plural(comments, 'new comment')} from {path}", 8000)
        self._refresh_datasets()
        if self.coverage is not None:
            self.coverage.refresh_saved()

    def _show_coverage(self) -> None:
        if not self.current:
            return
        if self.coverage is None:
            self.coverage = CoverageWindow(self)
        self.coverage.set_subreddit(self.current)
        self.coverage.set_busy(self.fetch_job.busy)
        opening = not self.coverage.isVisible()
        self.coverage.show()
        if opening:
            motion.fade_window(self.coverage)
        self.coverage.raise_()
        self.coverage.activateWindow()

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
        if self.coverage is not None:
            self.coverage.close()
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
        self.store.close()
        super().closeEvent(event)

    def running_threads(self) -> list[QThread]:
        """Fetch and count threads still running, such as one stuck on a slow request after the window
        closed."""
        jobs = [self.fetch_job, self.coverage and self.coverage.count_job]
        return [j.qthread for j in jobs if j and j.running()]
