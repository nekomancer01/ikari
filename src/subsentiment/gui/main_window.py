from __future__ import annotations

import os
import re
import sys
import threading
import time
from datetime import datetime
from functools import partial

import numpy as np
from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QSize, Qt, QThread, QTimer, QUrl, Slot
from PySide6.QtGui import QAction, QActionGroup, QDesktopServices, QFontMetrics, QGuiApplication, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QStackedWidget,
    QVBoxLayout, QWidget,
)

from subsentiment import __version__, collect
from subsentiment.analysis import Analysis
from subsentiment.gui import days, motion, native, theme
from subsentiment.gui.coverage import CoveragePage
from subsentiment.gui.fetch_form import FetchForm
from subsentiment.gui.charts import NoteDialog, TimelineChart
from subsentiment.gui.overview import OverviewPage, WordsPage
from subsentiment.gui.rows import RowList, Rows, Saved, chosen
from subsentiment.gui.words import KINDS, Rescore, WordTally
from subsentiment.gui.rail import DRAWERS, PAGES, NavRail
from subsentiment.gui.tables import COMMENT_COLUMNS, POST_COLUMNS, TablePage, marked
from subsentiment.gui.theme import mark, paint_backdrop
from subsentiment.gui.update_flow import UpdateFlow
from subsentiment.gui.widgets import (
    ElidedLabel, ProgressBar, QuietScrollBars, ShrinkingComboBox, StatusLabel, Throbber, fit_to_screen, label, panel,
    plural, section,
)
from subsentiment.gui.worker import (
    BackgroundJob, Collector, FetchWorker, LoadWorker, Slices, StoreRead, StoreWorker, ThreadScorer, finish,
)
from subsentiment.reddit import normalize_subreddit, subreddit_error
from subsentiment.store import Store, scoring_at_least


SCOPES = ["Posts and comments", "Posts only", "Comments only"]
# The header's score filters: label -> the lowest score shown (None for any), for posts and for comments.
MIN_SCORES = {"Any score": None, "Score 1+": 1, "Score 10+": 10, "Score 50+": 50, "Score 100+": 100,
              "Score 500+": 500, "Score 1,000+": 1000}
DRAWER_WIDTH = 318
DRIP_MS = (40, 250)  # the shortest and longest wait between lots of a fetch's rows put into the tables
NO_DATA = "Open Fetch on the left, enter a subreddit and press Fetch and analyse."


class FetchStrip(QWidget):
    """Under the header: the running fetch's progress, what it's doing and Stop, then what it did, until
    Hide. It takes no room while it has nothing to show."""

    def __init__(self):
        super().__init__()
        self.progress = ProgressBar()
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
    if not (result.posts or result.updated or result.already or result.comments):
        if result.stopped:
            return "Stopped before any posts were fetched."
        if result.timed_out:
            return f"Arctic Shift timed out on r/{result.subreddit} in this range; try again in a few minutes."
        matching = f" matching \u201c{req.query}\u201d" if req.query else ""
        return f"No new posts found in r/{result.subreddit}{matching} " + (
            "on the ticked days." if req.days else "in this date range.")
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
    SPAN_SETTLE_MS = 300  # the timeline's span held still this long has settled (see _on_view_changed)
    EXPORT_APART_FROM = 10_000  # rows; an export this big is written in a process of its own (Store.export)
    # A big saved subreddit to show at the start is read before the window is made. The app sets it False, so the
    # window shows at once and the subreddit fills in once it's read on a thread.
    wait_for_data = True

    @staticmethod
    def title_text() -> str:
        """"ikari", plus the checked-out commit in brackets when run from source (not a packaged build).

        Reads .git/HEAD and the ref it names rather than starting git, so it costs microseconds.
        """
        if getattr(sys, "frozen", False):
            return "ikari"
        folder = os.path.dirname(os.path.abspath(__file__))
        try:
            while not os.path.exists(os.path.join(folder, ".git")):
                if os.path.dirname(folder) == folder:
                    return "ikari"
                folder = os.path.dirname(folder)
            git = os.path.join(folder, ".git")
            if os.path.isfile(git):  # a worktree: ".git" is a file holding "gitdir: <path>"
                with open(git, encoding="utf-8") as f:
                    git = os.path.join(folder, f.read().split(":", 1)[1].strip())
            with open(os.path.join(git, "HEAD"), encoding="utf-8") as f:
                head = f.read().strip()
            if head.startswith("ref:"):
                ref = head.split(":", 1)[1].strip()
                common = git
                if os.path.exists(os.path.join(git, "commondir")):  # worktree refs live in the main repo
                    with open(os.path.join(git, "commondir"), encoding="utf-8") as f:
                        common = os.path.join(git, f.read().strip())
                try:
                    with open(os.path.join(common, ref), encoding="utf-8") as f:
                        head = f.read().strip()
                except FileNotFoundError:  # packed by git gc
                    with open(os.path.join(common, "packed-refs"), encoding="utf-8") as f:
                        head = next(line.split()[0] for line in f if line.rstrip().endswith(" " + ref))
        except (OSError, IndexError, StopIteration):
            return "ikari"
        return f"ikari ({head[:7]})" if re.fullmatch(r"[0-9a-f]{40}", head) else "ikari"

    def __init__(self, db_path: str):
        super().__init__()
        Collector.start()  # garbage is collected on this thread from here on, never on a reading thread
        self.setWindowTitle(self.title_text())
        self.resize(fit_to_screen(QSize(1320, 860), QGuiApplication.primaryScreen()))
        self.db_path = db_path
        self.store = Store(db_path)
        # Packs the text of data saved before day packs existed, without holding up the window (store.PACKED).
        # A database from before packs counted their words (version 3) has every row's text split for them, and so
        # do an import's rows (_on_import_ended): read meanwhile, a subreddit's words would be split row by row on
        # the reading thread, holding up the window (a second at a time over 36 s for 650,000 imported comments).
        self._wordless = self.store.db.execute("PRAGMA user_version").fetchone()[0] < 3
        self._upgrade_stop = threading.Event()
        self.upgrading = threading.Thread(target=Store.upgrade, args=(db_path, self._upgrade_stop), daemon=True)
        self.upgrading.start()
        # What's read while it runs leaves its words uncounted (rows.Saved): read again once it's done.
        self._upgraded = QTimer(self)
        self._upgraded.setInterval(500)
        self._upgraded.timeout.connect(self._after_upgrade)
        self._upgraded.start()
        self.settings = QSettings()
        self.updates = UpdateFlow(self, self.settings)
        self.fetch_job = BackgroundJob(self)
        self.fetch_job.ended.connect(self._on_fetch_ended)
        self.load_job = BackgroundJob(self)  # reads a big saved subreddit
        self.import_job = BackgroundJob(self)  # merges another database in, on a connection of its own
        self.import_job.ended.connect(self._on_import_ended)
        self._importing: tuple = ()  # (the file being imported, what import_from returned or the error)
        # Writes a CSV file in a process of its own (Store.export): 650,000 comments took 23 s, with the
        # window frozen.
        self.export_job = BackgroundJob(self)
        self.export_job.ended.connect(self._on_export_ended)
        self._exporting: tuple = ()  # (kind, file, what it kept, what export returned or the error)
        self._export_stop = threading.Event()  # the window is closing: the file isn't finished
        self.load_job.ended.connect(self._load_wanted)
        self._wanted: str | None = None  # the subreddit (lowercased) being read on load_job's thread, to show then
        self._stopping = False  # Stop was pressed on the running fetch
        self.current: str | None = None
        self.request: collect.FetchRequest | None = None
        self._live = QTimer(self)
        self._live.setSingleShot(True)
        self._live.setInterval(1000)
        self._live.timeout.connect(self._live_refresh)
        # The saved-data list's counts, read on a thread while a fetch runs: counting a big subreddit's comments took
        # a tenth of a second, once a second, on this one.
        self._sets_read = StoreRead(db_path, "datasets", self)
        self._sets_read.got.connect(self._on_sets_read)
        self._drip = QTimer(self)  # releases fetched rows into the tables a few at a time, so each is seen
        self._drip.setInterval(DRIP_MS[0])
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
        self._span_settled.setInterval(self.SPAN_SETTLE_MS)
        self._span_settled.timeout.connect(self._span_settle)
        self._span_slices = Slices(self)
        self._queued: dict[str, list] = {"posts": [], "comments": []}
        self._queued_ids: set[str] = set()
        self._since = 0.0  # rows saved since this moment belong to the running fetch
        # How far each table has read the running fetch's rows, so each save reads only what's new.
        self._read_to = {"posts": 0.0, "comments": 0.0}
        # The ids of rows the running fetch saved again that _count_new counted in: the tables show them anew once it ends.
        self._resaved: dict[str, list] = {"posts": [], "comments": []}
        self._follow = False  # show the subreddit being fetched, until the user picks another one
        # The overview's numbers of every saved day, of the rows that pass the score filters (from _saved).
        self._analysis = {"posts": Analysis(), "comments": Analysis()}
        self._last_fetched: float | None = None  # when the newest saved post of the subreddit shown was saved
        # Every saved post and comment of the subreddit shown (a rows.Saved each), kept up as a running fetch saves more.
        self._saved: dict = {"posts": RowList(), "comments": RowList()}
        # The span of days every page counts, as the timeline shows it: [lo, hi) in epoch seconds, None for
        # every day; and the timeline's range button it came from, if any.
        self.span: tuple[float, float] | None = None
        self._preset: str | None = "All"
        self._table_days: tuple[int, int] | None = None  # the whole days the tables were filtered to
        self._following = False  # the timeline is being given new days
        # The last few subreddits shown, (what the saved-data list said of it then (posts, comments, last fetched),
        # _saved) by name lowercased: switching back to one needn't read it again.
        self._loaded: dict[str, tuple] = {}
        self._stats: dict[str, tuple] = {}
        self.setWindowIcon(QIcon(str(theme.ASSETS / "app.png")))
        # A native material (Mica/Acrylic/vibrancy) needs a translucent window from the start,
        # so a change applies on next launch.
        self._native_backdrop = native.prepare_window(self, self.settings.value("ui/backdrop", "gradient", str))
        self._shown = False
        self._counted = Analysis()  # what the Overview shows
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
        # Anything the window waits on for over a second sweeps along the rule under the header.
        self.throbber = Throbber(self.rule, self.waiting, beside=self.subheading)
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
        # What the Overview counts.
        self.scope = mark(ShrinkingComboBox(), "pill", True)
        self.scope.addItems(SCOPES)
        self.scope.setToolTip("Count posts, comments or both on the Overview and Words")
        saved = self.settings.value("ui/overview_scope", SCOPES[0], str)
        self.scope.setCurrentIndex(max(self.scope.findText(saved), 0))
        self.scope.currentTextChanged.connect(self._on_scope_changed)
        # Which posts, and which comments, every page counts by their own score. It only hides, never deletes.
        self.score_pick = mark(ShrinkingComboBox(), "pill", True)
        self.comment_score_pick = mark(ShrinkingComboBox(), "pill", True)
        for pick, setting, noun in ((self.score_pick, "ui/min_score", "posts"),
                                    (self.comment_score_pick, "ui/min_comment_score", "comments")):
            for text, value in MIN_SCORES.items():
                pick.addItem(text if noun == "posts" else text.replace("Any score", "Any comments").replace("Score", "Comments"), value)
            pick.setToolTip(f"Count only {noun} scoring at least this much")
            pick.setCurrentIndex(max(pick.findText(self.settings.value(setting, "", str)), 0))
            pick.currentIndexChanged.connect(lambda _=0, p=pick, k=setting: self._on_min_score(p, k))
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
        # The scope shows on the Overview and Words only; a row keeps the score filter at the right edge
        # without a hidden column's spacing.
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addWidget(self.scope)
        controls.addWidget(self.score_pick)
        controls.addWidget(self.comment_score_pick)
        header.addLayout(controls, 0, 1, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.subheading, 1, 0, 1, 2)
        header.addWidget(self.fetch_strip, 2, 0, 1, 2)
        header.setColumnStretch(0, 1)
        lay.addLayout(header)
        self.rule = mark(QFrame(), "role", "divider")
        lay.addWidget(self.rule)

        self.pages = QStackedWidget()
        self.content = content
        self.overview = OverviewPage()
        self.overview.timeline.note_requested.connect(self._edit_note)
        self.overview.timeline.view_changed.connect(self._on_view_changed)
        self.words_page = WordsPage()
        self.word_tally = WordTally(self)
        self._words_for = None  # the subreddit whose words the clouds show
        self.word_tally.changed.connect(self._render_words)
        # What a word means in a subreddit: set beside the cloud, and the saved items using it scored again.
        self.rescore = Rescore(self.store, self.db_path, self)
        self.rescore.rescored.connect(self._on_rescored)
        self.rescore.progress.connect(self.words_page.meanings.show_progress)
        self.rescore.finished.connect(self.words_page.meanings.show_progress)
        self.rescore.scored.connect(self.words_page.meanings.show_result)
        self.rescore.finished.connect(self._on_rescore_finished)
        self.rescore.failed.connect(self.words_page.meanings.show_failed)
        self._set_mid_fetch: set[str] = set()  # words set in the subreddit a fetch is saving, scored again after it
        # The positions rescored (in the rows they're of), for the tables once it's done.
        self._rescored: dict[str, tuple] = {k: (None, []) for k in KINDS}
        self.words_page.meanings.changed.connect(self._set_word)
        self.overview.words.word_clicked.connect(self._pick_word)
        self.posts_tab = TablePage(POST_COLUMNS, stretch_col=2)
        self.comments_tab = TablePage(COMMENT_COLUMNS, stretch_col=2)
        self.tabs = {"posts": self.posts_tab, "comments": self.comments_tab}
        # The post title would otherwise size to its text and squeeze the comment column.
        post_col = self.comments_tab.view.horizontalHeader()
        post_col.setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        post_col.resizeSection(4, 180)
        self.coverage = CoveragePage(self)
        self.page_widgets = {"overview": self.overview, "words": self.words_page,
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
        self.scope.setVisible(key in ("overview", "words"))
        self.score_pick.setVisible(key != "coverage")
        self.comment_score_pick.setVisible(key != "coverage")  # Download coverage counts every saved post
        self.settings.setValue("ui/page", key)
        if key == "words":
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
            self.fetch_form.show_range()  # dates up to today, which may have begun since it last opened
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
        motion.crossfade(self, motion.THEME_MS, regrab_ms=None)
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

    def waiting(self) -> str | None:
        """What the window is waiting on, for the throbber: "" where the header already says (a subreddit being
        opened or filtered), None for nothing. While a fetch runs for the subreddit shown, the tables and words
        following it are its progress bar's to tell."""
        if self._wanted is not None:
            return ""
        table = self.posts_tab.busy() or self.comments_tab.busy()
        if table:
            return table
        if self.rescore.running():
            return "Scoring saved items\u2026"
        if self.coverage.delete_job.busy:
            return "Deleting days\u2026"
        if self.import_job.busy:
            return "Importing saved data\u2026"
        if self.export_job.busy:
            return f"Exporting {self._exporting[0]}\u2026"
        if self.coverage.reading() and self.coverage.isVisible():
            return "Reading saved days\u2026"
        if not (self.fetch_job.busy and self._showing_fetch()):
            if self._span_slices.running():
                return "Filling the tables\u2026"
            if self.word_tally.busy():
                return "Counting words\u2026"
        if self.upgrading.is_alive():
            return "Shrinking saved data\u2026"
        return None

    def set_writing(self) -> None:
        """Download coverage's deletion or an import started or ended on its thread: nothing else writes while one
        runs. An import holds the database for its one transaction (36 s for the owner's), so words' meanings and
        notes, saved from this thread, wait for it too."""
        self.fetch_btn.setEnabled(not self.writing())
        self._update_delete_enabled()
        self.coverage.set_busy(self.writing())
        self.words_page.meanings.setEnabled(not self.import_job.busy)

    def writing(self) -> bool:
        """A fetch, a deletion or an import is changing the saved data."""
        return self.fetch_job.busy or self.coverage.delete_job.busy or self.import_job.busy

    def _update_delete_enabled(self) -> None:
        idle = not self.writing()
        self.delete_btn.setEnabled(idle and bool(self.current))
        self.delete_act.setEnabled(idle and bool(self.current))
        self.delete_all_btn.setEnabled(idle and self.datasets.count() > 0)
        self.delete_all_act.setEnabled(idle and self.datasets.count() > 0)
        # Not beside the upgrade either: it packs what an import took, and the two would wait on each other.
        self.import_act.setEnabled(idle and not self.rescore.running() and not self.upgrading.is_alive())

    # -- fetching -------------------------------------------------------------

    def _start_fetch(self) -> None:
        if self.writing():
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
        if self.writing():
            return False
        self._launch(req, message)
        return True

    def _launch(self, req: collect.FetchRequest, message: str) -> None:
        worker = FetchWorker(req, self.db_path)
        worker.pack = not self.upgrading.is_alive()
        self.request = req
        self._since = worker.since
        self._read_to = {"posts": self._since, "comments": self._since}
        self._resaved = {"posts": [], "comments": []}
        self._flush_rows()
        self._follow = True
        self._open_fetched()  # if it's already saved, show it now; otherwise with its first rows
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
            self.coverage.clear_queue()  # Stop stops the downloads queued after it too
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

    def _on_saved(self, got: dict | None = None) -> None:
        # Rows were committed (``got``, read back by the fetch's thread): queue them for the tables right away, and
        # refresh the sidebar list and overview charts at most about once a second so big fetches stay smooth.
        if self._follow and not self._showing_fetch():
            self._open_fetched()
        self._queue_new_rows(got)
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

    def _showing_fetch(self) -> bool:
        return bool(self.request and self.current and self.current.lower() == self.request.subreddit.lower())

    def _queue_new_rows(self, got: dict | None = None) -> None:
        """Count in and queue what the fetch saved since the tables last took its rows: ``got`` as the fetch's
        thread read it back ({kind: (saved at or after, rows)}), or read here when that doesn't reach back far
        enough (the window was showing another subreddit when it was sent)."""
        if not self.fetch_job.busy or not self._showing_fetch():
            return
        name = self.current
        for kind, read in (("posts", self.store.posts_since), ("comments", self.store.comments_since)):
            since, rows = (got or {}).get(kind, (None, None))
            if since is None or since > self._read_to[kind]:
                rows = read(name, self._read_to[kind])
            elif since < self._read_to[kind]:  # read here since: only what's newer
                rows = [r for r in rows if r["fetched_at"] >= self._read_to[kind]]
            if rows:  # rows saved in the same instant as the last one read come back too; the ids catch them
                self._read_to[kind] = rows[-1]["fetched_at"]
                self._count_new(kind, rows)
                rows = scoring_at_least(rows, self.min_scores[kind == "comments"])
            for r in days.keep(rows, self._table_days):
                if r["id"] not in self._queued_ids:
                    self._queued_ids.add(r["id"])
                    self._queued[kind].append(r)
        if any(self._queued.values()) and not self._drip.isActive():  # start() would restart it, and
            self._drip.start(DRIP_MS[0])  # comments saved every few ms would then hold every row back

    def _count_new(self, kind: str, rows) -> None:
        """Count rows the running fetch saved into the subreddit shown, whatever their day or score: the timeline
        shows every day, and the span and score filters can be moved onto them later. Rows saved before, saved
        again with new scores, take the place of what they were."""
        rows = [r for r in rows if r["created_utc"] is not None]
        if rows:
            saved = self._saved[kind]
            ids = [r["id"] for r in rows]
            again = saved.where(ids) >= 0
            saved.add(rows)
            self._resaved[kind] += [i for i, a in zip(ids, again.tolist()) if a]
            if not self._shown_live.isActive():
                self._shown_live.start()

    def _release_rows(self) -> None:
        """Show the next few queued rows, the share of the backlog that drains it in about a second, so a long one
        never lags far behind. Rows put into a table of 100,000 take several ms, and the table is drawn again
        after, so the next lot waits about 25 times as long as this one took (from 25 lots a second for small
        tables to 4 for big ones): the window keeps its time for frames."""
        started = time.perf_counter()
        for kind, tab in self.tabs.items():
            queue = self._queued[kind]
            n = max(1, round(len(queue) * self._drip.interval() / 1000))
            batch, self._queued[kind] = queue[:n], queue[n:]
            if batch:
                self._add_rows(kind, tab, batch)
        if not any(self._queued.values()):
            self._drip.stop()
        self._update_dataset_labels()
        self._drip.setInterval(min(DRIP_MS[1], max(DRIP_MS[0], round((time.perf_counter() - started) * 25_000))))

    def _add_rows(self, kind: str, tab: TablePage, rows) -> None:
        tab.add_rows(rows)
        if kind == "posts" and rows:  # (a big table being worked out takes them once it's done)
            self._last_fetched = max([self._last_fetched or 0.0] + [r["fetched_at"] for r in rows])

    def _flush_rows(self) -> None:
        self._drip.stop()
        for kind, tab in self.tabs.items():
            if self._queued[kind]:
                self._add_rows(kind, tab, self._queued[kind])
            self._queued[kind] = []
        self._queued_ids.clear()

    def _update_dataset_labels(self) -> None:
        self.export_posts.setEnabled(bool(self.posts) and not self.export_job.busy)
        self.export_comments.setEnabled(bool(self.comments) and not self.export_job.busy)
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
        if self._follow and not self._showing_fetch():
            self._open_fetched()
        else:
            self._sets_read.ask()
        self.coverage.refresh_saved()

    def _on_sets_read(self, _asked, sets: list[dict]) -> None:
        if self.fetch_job.busy:  # (once it's over, the list is read here again)
            self._refresh_datasets(reload=False, sets=sets)

    def _on_finished(self, result: collect.FetchResult) -> None:
        self._loaded.clear()  # what it saved (scores too) is read again, not shown from memory
        self._rescore_after_fetch()
        self.progress_msg.setText(finish_text(result, self.request))
        # Download coverage's ticked days: unticked once read to the end; stopped, they stay, offered to resume.
        self.coverage.fetch_ended(self.request, result.stopped or result.timed_out)
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
        """Bring what the finished fetch saved into the dataset on screen: rows it saved again (new scores) and
        any not dripped in yet. Reading back only these, not every saved row, keeps a big subreddit from freezing
        the window for seconds as the fetch ends."""
        if self._span_slices.running():  # the tables are mid-way to a new span: finish it with the rows as they are
            self._apply_span()
        for kind, read, tab in (("posts", self.store.posts_since, self.posts_tab),
                                ("comments", self.store.comments_since, self.comments_tab)):
            # What it saved was counted in as it went (_count_new), and its new rows put in the tables: only what
            # came after needs reading, and only rows saved again showing anew.
            since = max(self._since, self._read_to[kind])
            rows = [r for r in read(self.current, since) if r["fetched_at"] > since or since == self._since]
            saved = self._saved[kind]
            saved.add(rows)
            # A row rescored below its score filter leaves the table; the rest show their new scores.
            at = chosen(saved, self._table_days, self.min_scores[kind == "comments"])
            tab.retain(at)
            fetched = saved.where(list(dict.fromkeys(self._resaved[kind] + [r["id"] for r in rows])))
            tab.took(fetched[np.isin(fetched, at)])
        self._loaded[self.current.lower()] = (self._stats.get(self.current.lower()), self._saved)
        self._last_fetched = self._newest("posts")
        self._update_dataset_labels()
        self._render_overview()

    def _on_failed(self, message: str) -> None:
        self._loaded.clear()
        self._rescore_after_fetch()
        self._live.stop()
        self._flush_rows()
        self.coverage.fetch_ended(self.request, True)
        self.coverage.clear_queue()  # the archive is likely failing the ones queued too
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

    def _refresh_datasets(self, select: str | None = None, reload: bool = True, sets: list[dict] | None = None) -> None:
        """Reload the saved-data list, selecting select, else the current dataset, else the newest.
        With reload=False only the list and storage line change, not the dataset on screen. ``sets``: the list as
        read on a thread (Store.datasets)."""
        if sets is None:
            self._sets_read.forget()  # older than this
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
        key, stats = name.lower(), self._stats.get(name.lower())
        loaded = self._loaded.pop(key, None)
        # What a running fetch saves into the subreddit shown is counted in as it's saved: it's up to date.
        kept = loaded is not None and loaded[1] is self._saved and self.fetch_job.busy and self._showing_fetch()
        if loaded is not None and loaded[0] != stats and not kept:  # fetched or deleted since: read again
            loaded = None
        if (loaded is None and not wait and (self.isVisible() or not self.wait_for_data) and stats
                and stats[0] + stats[1] >= self.LOAD_THREAD_FROM):
            self._wanted = key
            self.subheading.setText(f"Opening r/{name}…")
            self._load_wanted()
            return
        self._wanted = None
        if loaded is None:
            words = not (self._wordless and self.upgrading.is_alive())
            loaded = (stats, {k: Saved(self.db_path, k, name, words) for k in KINDS} if name else
                      {k: RowList() for k in KINDS})
        self._open(name, loaded)

    def _open(self, name: str, loaded: tuple) -> None:
        """Show subreddit ``name``, read: ``loaded`` is (what the saved-data list said of it, its rows.Saved)."""
        # Rows still queued from a running fetch belong to whatever was shown before; the tables are
        # about to be reloaded from the store, which has them anyway.
        self._drip.stop()
        self._queued = {"posts": [], "comments": []}
        switching = (name or None) != self.current
        if switching:  # the old subreddit fades out over the new one
            motion.crossfade(self.content)
            if self.fetch_job.busy and self._showing_fetch():  # what the fetch saves from now on isn't counted in
                self._loaded.pop(self.current.lower(), None)
        self.current = name or None
        if name:
            self._loaded[name.lower()] = loaded  # the newest last
            while len(self._loaded) > 4:
                del self._loaded[next(iter(self._loaded))]
        kept, self._saved = loaded[1] is self._saved, loaded[1]
        if self.fetch_job.busy and self._showing_fetch() and not kept:
            # What it saved after these were read: they read the database as it was when they were made.
            self._read_to = {k: max(self._since, getattr(self._saved[k], "made_at", 0.0) - 1.0) for k in KINDS}
        self._last_fetched = self._newest("posts")
        # The span remembered for it, shown on the timeline once they have its days.
        self.span, self._preset = days.load_range(self.settings, name)
        self._render_overview(restore=True)
        self._reload_span()
        self._show_notes()
        # (Deleting what was shown sets current to None first: not switching, but its words go too.)
        if switching or self.words_page.meanings.subreddit != self.current:
            self.words_page.meanings.set_subreddit(self.current, self.store.words(self.current) if self.current else {})
        self._update_delete_enabled()
        self.coverage.set_subreddit(self.current)

    @property
    def posts(self) -> Rows:
        """What the Posts table shows and export writes: the saved posts from the days in the span that pass the
        score filter (whatever the table's own filters show)."""
        return self.posts_tab.all_rows

    @property
    def comments(self) -> Rows:
        return self.comments_tab.all_rows

    def _newest(self, kind: str) -> float | None:
        """When the newest saved post or comment (``kind``) of the subreddit shown was saved."""
        if not self.current:
            return None
        return self.store.db.execute(f"SELECT MAX(fetched_at) FROM {kind} WHERE subreddit = ? COLLATE NOCASE",
                                     (self.current,)).fetchone()[0]

    def _load_wanted(self) -> None:
        """Read the subreddit last asked for on load_job's thread, once the one before is in."""
        if self._wanted is None or self.load_job.busy:
            return
        key = self._wanted
        worker = LoadWorker(self.db_path, key, (key, self._stats.get(key)),
                            words=not (self._wordless and self.upgrading.is_alive()))
        worker.read.connect(self._on_loaded)
        worker.failed.connect(self._on_load_failed)
        self.load_job.start(worker)

    def _on_loaded(self, read: tuple) -> None:
        (key, stats), saved = read
        if self._wanted == key:
            self._wanted = None
            self._open(self._name_of(key), (stats, saved))

    def _after_upgrade(self) -> None:
        """Once the upgrade is done, read again what was read without its words while it ran."""
        if self.upgrading.is_alive():
            return
        self._upgraded.stop()
        self._wordless = False
        self._update_delete_enabled()
        for key in [k for k, (_, saved) in self._loaded.items() if not all(getattr(s, "words", True) for s in saved.values())]:
            del self._loaded[key]
        wordless = not all(getattr(s, "words", True) for s in self._saved.values())
        if wordless and self.current and not (self.fetch_job.busy and self._showing_fetch()):
            self._show_dataset(self.current)

    def _on_load_failed(self, message: str) -> None:
        if self._wanted is not None:  # read it here instead, where an error shows as it always has
            self._show_dataset(self._name_of(self._wanted), wait=True)

    def _name_of(self, lowered: str) -> str:
        """A saved subreddit's name as the list writes it."""
        for i in range(self.datasets.count()):
            name = self.datasets.item(i).data(Qt.ItemDataRole.UserRole)
            if name.lower() == lowered:
                return name
        return lowered

    def _on_view_changed(self) -> None:
        """The timeline was zoomed, dragged or set to a range button: every page now counts its span."""
        chart = self.overview.timeline
        if (chart.view, chart.preset) == (self.span, self._preset):
            return
        self.span, self._preset = chart.view, chart.preset
        if self.current and not self._following:  # a fetch or the scope moved it, not the user
            days.save_range(self.settings, self.current, self.span, self._preset)
        self._render_span(moving=True)
        self._span_settled.start(self.SPAN_SETTLE_MS)

    def _show_span(self) -> None:
        """Show the counted days on the timeline and the span every page counts, without telling the window
        of it again."""
        chart = self.overview.timeline
        chart.blockSignals(True)
        chart.set_points(self._counted.daily_means)
        chart.show_span(self.span, self._preset)
        chart.blockSignals(False)

    def _span_settle(self) -> None:
        """The span stopped moving: what follows it at its own pace catches up, a step a frame. Not while the
        timeline still eases there (a range button's zoom): started under its last frames, that held them up."""
        easing = self.overview.timeline.zoom
        if easing.running():
            self._span_settled.start(max(1, easing.left()))
            return
        self._render_span()
        self._span_settled.stop()
        # Steps left over from a span before this one: the tables may still show it, so go through them all.
        self._span_slices.start(self._span_steps(reload=self._span_slices.running()), lambda: None)

    def _reload_span(self) -> None:
        """Fill the tables from the span's saved rows again: a big subreddit's over the next few frames."""
        if self.isVisible() and sum(map(len, self._saved.values())) >= self.LOAD_THREAD_FROM:
            self._span_settled.stop()
            self._span_slices.start(self._span_steps(reload=True), lambda: None)
        else:
            self._apply_span(reload=True)

    def _apply_span(self, reload: bool = False) -> None:
        """Point the tables (and so export) at the saved rows from the days in the span, if those changed."""
        self._span_settled.stop()
        reload = reload or self._span_slices.running()
        self._span_slices.stop()
        finish(self._span_steps(reload))
        self.word_tally.count(self._saved, self._table_days, self.min_scores)  # (unless counted as they are already)

    def _span_steps(self, reload: bool = False):
        """_apply_span's work, yielding between the steps that take a while (a big table's order is worked out away
        from the frames). Each table takes its rows from _saved in its own step, so rows a running fetch adds in
        between are in them."""
        whole = days.whole(self.span)
        if whole == self._table_days and not reload:
            return
        self._table_days = whole
        # Rows still queued are in _saved already, so the tables have them once reloaded.
        self._drip.stop()
        self._queued = {"posts": [], "comments": []}
        for kind, tab in self.tabs.items():
            saved = self._saved[kind]
            yield from tab.set_rows_steps(saved, chosen(saved, whole, self.min_scores[kind == "comments"]))
            yield
        self._update_dataset_labels()

    @property
    def min_scores(self) -> tuple[int | None, int | None]:
        """The lowest score shown for posts and for comments (None for any)."""
        return self.score_pick.currentData(), self.comment_score_pick.currentData()

    def _on_min_score(self, pick, setting: str) -> None:
        """A score filter changed: every page counts the rows that pass it, from what's in memory."""
        self.settings.setValue(setting, pick.currentText())
        if not self.current:
            return
        motion.crossfade(self.content)
        self._flush_rows()
        self._render_overview()
        self._reload_span()

    def _on_scope_changed(self, text: str) -> None:
        self.settings.setValue("ui/overview_scope", text)
        motion.crossfade(self.pages)
        self._render_overview()
        self._render_words()

    def _render_overview(self, restore: bool = False, moving: bool = False) -> None:
        """Show every saved day on the timeline, and the span's days on the tiles and distribution. With
        restore, the timeline shows the span the window holds (another subreddit's, as remembered); with moving,
        the tint and word clouds are left to catch up later (`_render_span`)."""
        scope = self.scope.currentText()
        self._analysis = {k: self._saved[k].analysis(least) for k, least in zip(KINDS, self.min_scores)}
        self._counted = Analysis.combined(
            *[self._analysis[k] for k, skip in (("posts", "Comments only"), ("comments", "Posts only")) if scope != skip])
        chart = self.overview.timeline
        if restore:
            self._show_span()
            self.span, self._preset = chart.view, chart.preset
        else:  # a fetch adding days can move a span that ends on the latest day: _on_view_changed follows it
            self._following = True
            try:
                chart.set_points(self._counted.daily_means)
            finally:
                self._following = False
        self._render_span(moving=moving, ease=restore)

    def _render_span(self, moving: bool = False, ease: bool = False) -> None:
        """The tiles and distribution for the span's days. The background's tint (eased to another subreddit's)
        and the word clouds follow too, unless the span is being moved: they catch up once it settles."""
        a = self._counted.between(days.whole(self.span))
        self.overview.show_analysis(a)
        if not moving:
            self.mood.set_counts(a.counts["positive"], a.counts["negative"], a.count, ease)
            self.word_tally.count(self._saved, days.whole(self.span), self.min_scores)
            self._render_words()

    def _render_words(self) -> None:
        """The word clouds of the span's items the scope counts: 50 words in the Overview's card, 150 on the
        page. They follow the span once it settles, as the tables do, and ease from the words shown to the new
        ones. While a big recount runs the tally still has the counts from before it, so the subreddit's words
        stay up, faded, to ease from once it's done; another subreddit's aren't shown meanwhile."""
        scope = self.scope.currentText()
        kinds = [k for k, skip in zip(KINDS, ("Comments only", "Posts only")) if scope != skip]
        busy = not self.word_tally.ready
        words = [] if busy and self._words_for != self.current else self.word_tally.top(kinds, 150)
        if not busy:
            self._words_for = self.current
        meant = self.store.lexicon(self.current).weights if self.current else {}
        self.overview.words.set_words(words[:50], busy, meant)
        self.words_page.body.set_words(words, busy, meant)  # placed only once it shows

    def _show_notes(self) -> None:
        notes = self.store.notes(self.current) if self.current else {}
        self.overview.timeline.set_notes(notes)

    def _edit_note(self, day: int) -> None:
        """Add, change or remove the note on a day the timeline's dot was clicked on."""
        if not self.current:
            return
        if self.import_job.busy:
            self.statusBar().showMessage("Notes can be added once the import is done.", 5000)
            return
        dialog = NoteDialog(day, *self.store.notes(self.current).get(day, ("", "")), parent=self)
        dialog.chosen.connect(partial(self._save_note, self.current, day))
        dialog.open()

    def _save_note(self, subreddit: str, day: int, tag: str, text: str) -> None:
        self.store.save_note(subreddit, day, tag, text)
        self._show_notes()

    def _pick_word(self, word: str) -> None:
        """A word clicked in the Overview's cloud: set what it means on the Words page."""
        self.show_page("words")
        self.words_page.meanings.pick(word)

    def _set_word(self, word: str, weight: float | None) -> None:
        sub = self.words_page.meanings.subreddit
        if sub:
            self.store.save_word(sub, word, weight)
            self._render_words()  # hovering it in the clouds tells what it's set to
            # The rows on screen are all there is of it, unless a score filter or a running fetch leaves some out.
            fetching = self.fetch_job.busy and self.request is not None and self.request.subreddit.lower() == sub.lower()
            if fetching:  # it may be saving items scored the old way as this runs
                self._set_mid_fetch.add(word)
            here = not fetching and self.current is not None and self.current.lower() == sub.lower()
            self.rescore.start(sub, {word}, self._saved if here else None)

    def _rescore_after_fetch(self) -> None:
        """Items the fetch saved while words were being set were scored the old way: go through them again."""
        if self._set_mid_fetch:
            self.rescore.start(self.request.subreddit, self._set_mid_fetch)
            self._set_mid_fetch = set()

    def _on_rescored(self, sub: str, kind: str, rows: list) -> None:
        """Saved items scored again by a subreddit's words: the timeline and tiles follow, as with a fetch's rows.
        The word clouds and the background's tint catch up once it's done: easing them to every batch had the
        cloud drawn over a thousand times."""
        mine = self.current and self.current.lower() == sub
        if not mine:
            self._loaded.pop(sub, None)  # read again when next shown
            return
        saved = self._saved[kind]
        at = saved.rescore([(r["id"], r["sentiment"], r["label"]) for r in rows], [r["words"] for r in rows])
        source, done = self._rescored[kind]
        self._rescored[kind] = (saved, (done if source is saved else []) + [at[at >= 0]])
        self._render_overview(moving=True)

    def _on_rescore_finished(self, sub: str) -> None:
        """The tables (and what export writes) take the rescored rows in one go: each update sorts a table again.
        The word clouds and tint follow."""
        rescored, self._rescored = self._rescored, {k: (None, []) for k in KINDS}
        if self.current and self.current.lower() == sub:
            took = False
            for kind, tab in self.tabs.items():
                source, done = rescored[kind]
                if source is self._saved[kind] and done:
                    at = np.unique(np.concatenate(done))
                    tab.took(at[marked(tab.all_rows.at, at)])
                    took = True
            if took:  # (the timeline and tiles have each batch already)
                self._render_span()

    def days_deleted(self, subreddit: str, days: int, posts: int, comments: int) -> None:
        """Download coverage deleted some days of ``subreddit``: if it's still shown, show it again from the store
        on every page. If nothing of it is left it stays shown, empty, so its days can be downloaded again from
        here. Another subreddit picked meanwhile stays shown."""
        self._loaded.pop(subreddit.lower(), None)
        shown = (self.current or "").lower() == subreddit.lower()
        self._refresh_datasets(select=subreddit if shown else None, reload=False)
        if shown:
            if subreddit.lower() not in self._stats:
                self.datasets.blockSignals(True)
                self.datasets.setCurrentRow(-1)  # it has no row any more
                self.datasets.blockSignals(False)
            self._show_dataset(self.current)
        self.statusBar().showMessage(f"Deleted {plural(posts, 'post')} and {plural(comments, 'comment')} saved from "
                                     f"{plural(days, 'day')} of r/{subreddit}.", 8000)

    def _delete_dataset(self) -> None:
        if not self.current or self.writing():
            return
        if QMessageBox.question(
                self, "Delete saved data",
                f"Delete the {plural(len(self._saved['posts']), 'post')} and "
                f"{plural(len(self._saved['comments']), 'comment')} saved for "
                f"r/⁠{self.current}"  # a word joiner: "r/" doesn't wrap away from the name
                f"{', and its timeline notes' if self.store.notes(self.current) else ''}? "
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
        if self.writing() or not self.datasets.count():
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
        if not self.current or self.export_job.busy:
            return
        self._apply_span()  # the span in view now, not as it was before the last zoom settled
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind}", f"{self.current}_{kind}.csv", "CSV files (*.csv)")
        if not path:
            return
        chosen = None if self._table_days is None else {d for d in range(*self._table_days, days.DAY)}
        min_score = self.min_scores[kind == "comments"]
        on = "" if chosen is None else (f" from {days.short_date(self._table_days[0], True)} to "
                                        f"{days.short_date(self._table_days[1] - days.DAY, True)}")
        if min_score is not None:
            on += f" scoring {min_score:,}+"
        apart = len(self.tabs[kind].all_rows) >= self.EXPORT_APART_FROM  # (a process takes a moment to start)
        worker = StoreWorker(self.db_path, "export", self.current, kind, path, chosen, min_score, self._export_stop, apart)
        worker.finished.connect(self._exported)
        worker.failed.connect(self._exported)
        self._exporting = (kind, path, on, None)
        self.export_job.start(worker)
        self.export_posts.setEnabled(False)
        self.export_comments.setEnabled(False)
        self.statusBar().showMessage(f"Exporting {kind}{on} to {path}\u2026")

    @Slot(object)
    def _exported(self, got) -> None:
        """How many rows export wrote, or the error it raised."""
        self._exporting = (*self._exporting[:3], got)

    def _on_export_ended(self) -> None:
        kind, path, on, got = self._exporting
        self.export_posts.setEnabled(bool(self.posts))
        self.export_comments.setEnabled(bool(self.comments))
        if isinstance(got, int):
            self.statusBar().showMessage(f"Exported {got:,} {kind}{on} to {path}", 8000)
            return
        self.statusBar().clearMessage()
        # On Windows, usually the file is still open in Excel.
        why = got.removeprefix("OSError: ") if got else "It stopped unfinished."
        QMessageBox.warning(self, "Export failed", f"Couldn't save {path}.\n\n{why}\n\n"
                                                    "If the file is open in another program, close it and try again.")

    def _import_database(self) -> None:
        """Merge another ikari database (one saved under another account or computer) into this one."""
        if not self.import_act.isEnabled():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Import database", str(self.store.path.parent),
                                              "ikari databases (*.db);;All files (*)")
        if not path:
            return
        # On a thread: the owner's 230 MB database took 36 s, with the window frozen.
        worker = StoreWorker(self.db_path, "import_from", path)
        worker.finished.connect(self._imported)
        worker.failed.connect(self._imported)
        self._importing = (path, None)
        self.import_job.start(worker)
        self.set_writing()
        self.statusBar().showMessage(f"Importing {path}\u2026")

    @Slot(object)
    def _imported(self, got) -> None:
        """What import_from returned, or the error it raised."""
        self._importing = (self._importing[0], got)

    def _on_import_ended(self) -> None:
        path, got = self._importing
        self.set_writing()
        if not isinstance(got, tuple):
            self.statusBar().clearMessage()
            QMessageBox.warning(self, "Import failed", f"Couldn't import {path}.\n\n{got or 'It stopped unfinished.'}")
            return
        self._loaded.clear()
        self._wordless = True  # what it took is read without its words until it's packed (_after_upgrade)
        self.statusBar().showMessage(
            f"Imported {plural(got[0], 'new post')} and {plural(got[1], 'new comment')} from {path}", 8000)
        # What it took is packed there, as an older database's rows are (import is off till it's done).
        self.upgrading = threading.Thread(target=Store.upgrade, args=(self.db_path, self._upgrade_stop), daemon=True)
        self.upgrading.start()
        self._upgraded.start()
        self._refresh_datasets()  # (Download coverage read what's saved again as set_writing let it go)

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
            worker.stop(closing=True)
            for sig in (worker.progress, worker.finished, worker.failed, worker.saved, thread.finished):
                sig.disconnect()
            thread.quit()
            thread.wait(3000)
            # Signals it sent just before are already queued, and disconnecting doesn't take them back.
            QCoreApplication.removePostedEvents(self, QEvent.Type.MetaCall)
        ThreadScorer.close()  # its idle processes would keep the app from exiting
        self._live.stop()  # both read the store
        self._sets_read.close()
        self._drip.stop()
        self._shown_live.stop()
        self._span_settled.stop()
        self._span_slices.stop()
        self.word_tally.hold()
        self._wanted = None
        self._upgraded.stop()
        if self.load_job.busy:  # what it read is let go; its thread is waited for, as it uses the file
            for sig in (self.load_job.worker.read, self.load_job.worker.finished, self.load_job.worker.failed):
                sig.disconnect()
            self.load_job.worker.stop()
            self.load_job.qthread.quit()
            # A subreddit's index is a few seconds' reading at most (3 s for 650,000 comments while the upgrade
            # runs), and Qt aborts the process if the thread is still going when the window goes.
            self.load_job.qthread.wait()
        self.rescore.stop()
        # Each is one transaction, of which nothing is shown now. A deletion is let end, rather than cut short; an
        # import can take a minute, so app.main ends the process instead and SQLite rolls it back.
        # An export writing its file stops within a few thousand rows and leaves none; one still reading the rows
        # (half its time) hasn't started the file, so app.main can end the process under it.
        self._export_stop.set()
        for writing, wait in ((self.coverage.delete_job, 10000), (self.import_job, 0), (self.export_job, 1000)):
            if writing.busy:
                writing.worker.finished.disconnect()
                writing.worker.failed.disconnect()
                writing.ended.disconnect()
                writing.qthread.quit()
                writing.qthread.wait(wait)
        self._upgrade_stop.set()  # it stops after the day it's packing
        self.upgrading.join()
        self.store.close()
        super().closeEvent(event)

    def running_threads(self) -> list[QThread]:
        """Fetch and count threads still running, such as one stuck on a slow request after the window
        closed."""
        return [j.qthread for j in (self.fetch_job, self.coverage.count_job, self.coverage.delete_job,
                                    self.coverage.days_read.job, self.coverage.posts_read.job, self.load_job,
                                    self.rescore.job, self.import_job, self.export_job)
                if j.running()]
