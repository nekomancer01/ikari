from __future__ import annotations

import time
from datetime import datetime, timedelta

from PySide6.QtCore import (
    QDate, QEvent, QObject, QRectF, QSettings, QSize, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QIcon, QKeySequence,
    QPainter,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QApplication, QCheckBox, QComboBox, QDateEdit, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QStackedWidget, QTableView, QTabBar,
    QVBoxLayout, QWidget,
)

from subsentiment import __version__, collect
from subsentiment.gui import days, native, theme
from subsentiment.gui.charts import DistributionChart, TimelineChart, palette
from subsentiment.gui.models import COMMENT_COLUMNS, POST_COLUMNS, SORT_ROLE, RowsModel
from subsentiment.gui.theme import mark, paint_backdrop
from subsentiment.gui.update_flow import UpdateFlow
from subsentiment.gui.worker import FetchWorker
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


class ElidedLabel(QLabel):
    """One line that ends in "…" when there isn't room (the tooltip has it all), instead of wrapping.

    A wrapped label in the main window's layout gets no extra height for its second line, so it was
    cut off on narrow windows; and an unwrapped one would hold the window wider than small screens."""

    def __init__(self, text: str, role: str):
        super().__init__()
        self.setProperty("role", role)
        self._full = ""
        self.setText(text)

    def setText(self, text: str) -> None:
        self._full = text
        self._elide()

    def full_text(self) -> str:
        return self._full

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self._full) + 2, super().sizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._elide()

    def changeEvent(self, event) -> None:
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, max(self.width(), 1))
        super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class VerticalScrollArea(QScrollArea):
    """Scrolls up and down only, and keeps the window at least as wide as its contents need."""

    def __init__(self):
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def minimumSizeHint(self) -> QSize:
        inner = self.widget().minimumSizeHint() if self.widget() else QSize(0, 0)
        return QSize(inner.width() + self.verticalScrollBar().sizeHint().width(), 200)


class ShrinkingComboBox(QComboBox):
    """Full width when there's room; on a narrow window it gives up width (its text ends in "…")."""

    def minimumSizeHint(self) -> QSize:
        full = super().minimumSizeHint()
        return QSize(min(full.width(), 120), full.height())


class StatusLabel(ElidedLabel):
    """The muted line under Fetch: one line that ends in "…" (the tooltip has it all), no room while empty.

    It used to wrap, and a long "Added … Updated …" message took three lines, which pushed the sidebar
    into scrolling and shifted its fields."""

    def __init__(self):
        super().__init__("", "muted")
        self.setVisible(False)

    def setText(self, text: str) -> None:
        super().setText(text)
        self.setVisible(bool(text))

    def text(self) -> str:
        return self.full_text()


class QuietScrollBars(QObject):
    """Shows a list's scroll bar handle only while the pointer is over the list, like macOS overlay bars.

    The bar keeps its place, so text doesn't shift when it appears."""

    def __init__(self, area: QAbstractScrollArea):
        super().__init__(area)
        self._bar = area.verticalScrollBar()
        self._bar.setProperty("quiet", True)
        self._show(False)
        area.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:
        if event.type() in (QEvent.Type.Enter, QEvent.Type.Leave):
            self._show(event.type() == QEvent.Type.Enter)
        return False

    def _show(self, on: bool) -> None:
        self._bar.setProperty("hot", on)
        self._bar.style().unpolish(self._bar)
        self._bar.style().polish(self._bar)
        self._bar.update()


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
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(4)
        self.value = label("–", "value")
        lay.addWidget(label(caption.upper(), "section"))
        lay.addWidget(self.value)
        # Tiles without a meter keep an empty slot its size, so every caption and value lines up.
        self.meter = Meter(meter) if meter else None
        lay.addSpacing(4)
        if self.meter:
            lay.addWidget(self.meter)
        else:
            slot = QWidget()
            slot.setFixedHeight(6)
            lay.addWidget(slot)

    def set(self, text: str, share: float | None = None) -> None:
        self.value.setText(text)
        if self.meter is not None:
            self.meter.set_share(share or 0.0)


class TablePage(QWidget):
    """Filterable, sortable table on a solid panel; double-click opens the item on Reddit."""

    SHRINKS = ("author", "post_title")  # free text that can end in "…" to leave room for the main text
    OPTIONAL = ("author", "post_title", "num_comments")  # hidden in this order when there's still no room

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

    def add_rows(self, rows) -> int:
        """Append rows a running fetch has just saved; they fade in. Returns how many were new."""
        have = {r["id"] for r in self._all_rows}
        new = [r for r in rows if r["id"] not in have]
        label = self.label_filter.currentText()
        self._all_rows.extend(new)
        shown = new if label == "All labels" else [r for r in new if r["label"] == label.lower()]
        self.model.append_rows(shown)
        self._update_count()
        return len(new)

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
        self._widths = {}  # column -> (width for its values, narrowest that still shows its title)
        for col in range(self.model.columnCount()):
            if col != self.stretch_col:
                title = self.model.headerData(col, Qt.Orientation.Horizontal)
                least = metrics.horizontalAdvance(title) + 16 + 24 + 8  # padding, sort arrow, slack
                self._widths[col] = (max(self.view.sizeHintForColumn(col), least), least)
        self._share_width()

    def _share_width(self) -> None:
        """Keep room for the text column (the title, the comment). A comment's post title and the author
        used to take so much that the comment itself showed as "T…".

        First the free-text columns (author, post title) give up width down to their titles and their
        values end in "…"; if the text column still has under 40% of the width, the least needed columns
        are hidden, author first."""
        widths = getattr(self, "_widths", None)
        if not widths:
            return
        keys = {col: self.model.columns[col][1] for col in widths}
        room = self.view.viewport().width()
        hidden = set()
        for key in (None, *self.OPTIONAL):
            if key is not None:
                hidden |= {col for col, k in keys.items() if k == key}
            shown = {col: wl for col, wl in widths.items() if col not in hidden}
            fixed = sum(w for col, (w, _) in shown.items() if keys[col] not in self.SHRINKS)
            loose = {col: wl for col, wl in shown.items() if keys[col] in self.SHRINKS}
            spare = sum(w - least for w, least in loose.values())
            over = fixed + sum(w for w, _ in loose.values()) - room * 0.6
            give = min(1.0, over / spare) if over > 0 and spare > 0 else (0.0 if over <= 0 else 1.0)
            used = fixed + sum(round(w - (w - least) * give) for w, least in loose.values())
            if room - used >= room * 0.4 or key == self.OPTIONAL[-1]:
                break
        header = self.view.horizontalHeader()
        for col, (w, least) in widths.items():
            header.setSectionHidden(col, col in hidden)
            header.resizeSection(col, round(w - (w - least) * give) if keys[col] in self.SHRINKS else w)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._share_width()

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
        self._stopping = False  # Stop was pressed on the running fetch
        self.current: str | None = None
        self.request: collect.FetchRequest | None = None
        self._live = QTimer(self)
        self._live.setSingleShot(True)
        self._live.setInterval(1000)
        self._live.timeout.connect(self._live_refresh)
        self._drip = QTimer(self)  # releases fetched rows into the tables a few at a time, so each is seen
        self._drip.setInterval(40)
        self._drip.timeout.connect(self._release_rows)
        self._queued: dict[str, list] = {"posts": [], "comments": []}
        self._queued_ids: set[str] = set()
        self._since = 0.0  # rows saved since this moment belong to the running fetch
        # How far each table has read the running fetch's rows, so each save reads only what's new.
        self._read_to = {"posts": 0.0, "comments": 0.0}
        self._follow = False  # show the subreddit being fetched, until the user picks another one
        self.posts: list = []  # what the analysis counts: the saved rows from the chosen days
        self.comments: list = []
        self._saved: dict[str, list] = {"posts": [], "comments": []}  # every saved row of the subreddit shown
        self._day_counts: dict[int, tuple[int, int]] = {}
        self.chosen_days: set[int] | None = None  # None: every day
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
        lay.setContentsMargins(19, 16, 19, 16)
        lay.setSpacing(10)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        # The scroll bar sits in the right margin when it shows, so the fields don't shift left.
        bar = scroll.verticalScrollBar()
        bar.rangeChanged.connect(
            lambda _lo, hi: lay.setContentsMargins(19, 16, 19 - (bar.sizeHint().width() if hi > 0 else 0), 16))

        brand = QVBoxLayout()
        brand.setSpacing(2)
        brand.addWidget(label("ikari", "title"))
        brand.addWidget(label("Gather posts and read the mood", "subtitle"))
        lay.addLayout(brand)

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
        row.setSpacing(6)  # Windows' wider text needs the "50 per post" box this little bit wider
        row.addWidget(self.include_comments)
        row.addWidget(self.comments_per_post, 1)
        self._restore_fetch_settings()

        lay.addWidget(section("Fetch"))
        form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)
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
        # Each label as tall as its field and centred in it: QFormLayout top-aligns a short label, which put
        # labels a few pixels above their fields' centres where the text is small (macOS).
        for row in range(form.rowCount()):
            name = form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            name.setMinimumHeight(field.sizeHint().height())
            name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

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
        self.progress_msg = StatusLabel()
        lay.addWidget(self.progress)
        lay.addWidget(self.progress_msg)

        self.delete_btn = mark(mark(QPushButton("Delete…"), "flat", True), "small", True)
        self.delete_btn.setToolTip("Delete the saved posts and comments for the selected subreddit")
        self.delete_btn.clicked.connect(self._delete_dataset)
        self.delete_all_btn = mark(mark(QPushButton("Delete all…"), "flat", True), "small", True)
        self.delete_all_btn.setToolTip("Delete every saved subreddit")
        self.delete_all_btn.clicked.connect(self._delete_all)
        # The Delete buttons sit on the section's title row, so the panel fits a 150% maximized window.
        saved_head = QHBoxLayout()
        saved_head.setSpacing(2)
        saved_head.addWidget(section("Saved data"), 1)
        saved_head.addWidget(self.delete_btn)
        saved_head.addWidget(self.delete_all_btn)
        lay.addLayout(saved_head)
        self.datasets = QListWidget()
        self.datasets.setFrameShape(QFrame.Shape.NoFrame)
        # Fills whatever height is left, down to one row. Its default preferred height (about 190 px)
        # counted in full and pushed the storage line below a 150% maximized window, and at two rows a
        # status line under Fetch did the same.
        self.datasets.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self.datasets.setMinimumHeight(42)
        QuietScrollBars(self.datasets)
        self.datasets.currentItemChanged.connect(self._on_dataset_selected)
        lay.addWidget(self.datasets, 1)
        self.storage_info = label("", "muted")
        self.storage_info.setWordWrap(True)
        self.storage_info.setToolTip("Saved on this computer, kept between sessions")
        lay.addWidget(self.storage_info)
        return side

    def _build_content(self) -> QWidget:
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(14)

        self.heading = ElidedLabel("No data yet", "heading")
        self.subheading = ElidedLabel("Enter a subreddit on the left and press Fetch and analyse.", "subtitle")

        self.tabbar = QTabBar()
        self.tabbar.setDrawBase(False)
        self.tabbar.setExpanding(False)
        self.tabbar.setUsesScrollButtons(False)  # all three tabs always show
        self.tabbar.setElideMode(Qt.TextElideMode.ElideRight)  # at the narrowest, long counts end in "…"
        for t in ("Overview", "Posts", "Comments"):
            self.tabbar.addTab(t)
        # What the whole Overview (tiles, charts, strongest lists, mood tint) counts.
        self.scope = ShrinkingComboBox()
        self.scope.addItems(SCOPES)
        self.scope.setToolTip("Count posts, comments or both on the Overview")
        saved = self.settings.value("ui/overview_scope", SCOPES[0], str)
        self.scope.setCurrentIndex(max(self.scope.findText(saved), 0))
        self.scope.currentTextChanged.connect(self._on_scope_changed)
        # Which saved days every view counts (the Overview, both tables, CSV export). Picking never fetches.
        self.days_pick = days.DaysCombo()
        self.days_pick.chosen_changed.connect(self._on_days_chosen)
        self.heading.setMinimumWidth(QFontMetrics(theme.display_font(15, 650)).horizontalAdvance("r/subreddit"))
        # The title, the Overview's scope picker, the day picker and the tabs share one centre line, and the
        # subtitle runs the full width under them. It used to share a narrow column with the title and wrapped
        # at 150%, cutting off "last fetched". The day picker sits by the tabs, which it filters too, so it
        # stays put when the scope picker hides on the other tabs.
        header = QGridLayout()
        header.setHorizontalSpacing(12)
        header.setVerticalSpacing(2)
        header.addWidget(self.heading, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.scope, 0, 1, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.days_pick, 0, 2, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.tabbar, 0, 3, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.subheading, 1, 0, 1, 4)
        header.setColumnStretch(0, 1)
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
        widest = max(t.minimumSizeHint().width() for t in self.tiles.values())
        for t in self.tiles.values():
            t.setMinimumWidth(widest)  # equal widths, whatever the caption length, down to the narrowest window
            tiles.addWidget(t, 1)
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
            QuietScrollBars(lw)
            lw.itemDoubleClicked.connect(
                lambda item: QDesktopServices.openUrl(QUrl(item.data(Qt.ItemDataRole.UserRole))))
            card, inner = panel("solid", margins=14, spacing=6)
            inner.addWidget(label(title, "muted"))
            inner.addWidget(lw)
            extremes.addWidget(card)
        lay.addLayout(extremes, 2)
        # On a short window (1366x768 or 1280x800 at 125%) the overview scrolls rather than holding the
        # window taller than the screen or squeezing the charts.
        scroll = VerticalScrollArea()
        scroll.setWidget(page)
        return scroll

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
        self.fetch_btn.setEnabled(not busy)
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
        if self.thread is not None:
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
        self._since = time.time() - 1.0
        self._read_to = {"posts": self._since, "comments": self._since}
        self._flush_rows()
        self._follow = True
        self._open_fetched()  # if it's already saved, show it now; otherwise with its first rows
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
        self._stopping = False
        self.progress_msg.setText(message)
        self.thread.start()

    def _stop_fetch(self) -> None:
        if self.worker:
            self.worker.stop()
            self._stopping = True
            self.progress_msg.setText("Stopping…")
            if self.coverage is not None:
                self.coverage.set_progress(0, 0, "Stopping…")
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
        if self.thread is None or not self._showing_fetch():
            return
        name = self.current
        for kind, read in (("posts", self.store.posts_since), ("comments", self.store.comments_since)):
            rows = read(name, self._read_to[kind])
            if rows:  # rows saved in the same instant as the last one read come back too; the ids catch them
                self._read_to[kind] = rows[-1]["fetched_at"]
            for r in days.keep(rows, self.chosen_days):
                if r["id"] not in self._queued_ids:
                    self._queued_ids.add(r["id"])
                    self._queued[kind].append(r)
        if any(self._queued.values()) and not self._drip.isActive():  # start() would restart it, and
            self._drip.start()  # comments saved every few ms would then hold every row back

    def _release_rows(self) -> None:
        """Show the next few queued rows; a long backlog is released faster so it never lags far behind."""
        for kind, tab in (("posts", self.posts_tab), ("comments", self.comments_tab)):
            queue = self._queued[kind]
            batch, self._queued[kind] = queue[:max(1, len(queue) // 25)], queue[max(1, len(queue) // 25):]
            if batch:
                tab.add_rows(batch)
        if not any(self._queued.values()):
            self._drip.stop()
        self._update_dataset_labels()

    def _flush_rows(self) -> None:
        self._drip.stop()
        for kind, tab in (("posts", self.posts_tab), ("comments", self.comments_tab)):
            if self._queued[kind]:
                tab.add_rows(self._queued[kind])
            self._queued[kind] = []
        self._queued_ids.clear()

    def _update_dataset_labels(self) -> None:
        self.tabbar.setTabText(1, f"Posts  {len(self.posts):,}")
        self.tabbar.setTabText(2, f"Comments  {len(self.comments):,}")
        self.export_posts.setEnabled(bool(self.posts))
        self.export_comments.setEnabled(bool(self.comments))
        if not self.current:
            self.heading.setText("No data yet")
            self.subheading.setText("Enter a subreddit on the left and press Fetch and analyse.")
            return
        self.heading.setText(f"r/{self.current}")
        fetched = max((r["fetched_at"] for r in self._saved["posts"] + self.posts), default=None)
        on = ""
        if self.chosen_days is not None:
            n = len(self.chosen_days & set(self._day_counts))
            on = f" on {plural(n, 'chosen day')}"
        self.subheading.setText(f"{plural(len(self.posts), 'post')} and {plural(len(self.comments), 'comment')}{on} · "
                                f"last fetched {when_text(fetched)}")

    def _live_refresh(self) -> None:
        if self.thread is None or self.request is None:
            return
        if self._showing_fetch():
            self._refresh_datasets(reload=False)
            self._refresh_days()
            self._render_overview()
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
            self._show_dataset(self.current, animate=True)  # keeps the tables where they are
        elif result.posts or result.updated or result.comments:
            self._refresh_datasets(select=result.subreddit)

    def _on_failed(self, message: str) -> None:
        self._live.stop()
        self._flush_rows()
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
            f"{plural(posts, 'post')}, {plural(comments, 'comment')} · {human_size(self.store.size_bytes())}" if sets
            else "Nothing saved yet. Fetched data is kept on this computer between sessions.")
        self._update_delete_enabled()

    def _on_dataset_selected(self, item: QListWidgetItem | None) -> None:
        name = item.data(Qt.ItemDataRole.UserRole) if item else ""
        if self.thread is not None and self.request and name.lower() != self.request.subreddit.lower():
            self._follow = False  # the user chose to look at something else while it runs
        self._show_dataset(name)
        if name:
            self.settings.setValue("ui/last_dataset", name)

    def _show_dataset(self, name: str, animate: bool = False) -> None:
        """Show a saved subreddit. With animate (a fetch adding to it), new rows fade in."""
        # Rows still queued from a running fetch belong to whatever was shown before; the tables are
        # about to be reloaded from the store, which has them anyway.
        self._drip.stop()
        self._queued = {"posts": [], "comments": []}
        self.current = name or None
        self._saved = {"posts": self.store.posts(name) if name else [],
                       "comments": self.store.comments(name) if name else []}
        self.chosen_days = days.load_choice(self.settings, name) if name else None
        self._refresh_days()
        self._apply_days(animate)
        self._update_delete_enabled()
        self.coverage_act.setEnabled(bool(self.current))
        if self.coverage is not None:
            self.coverage.set_subreddit(self.current)

    def _refresh_days(self) -> None:
        """Re-read which days have saved data, for the day picker."""
        self._day_counts = self.store.day_counts(self.current) if self.current else {}
        self.days_pick.set_days(self._day_counts, self.chosen_days)

    def _apply_days(self, animate: bool = False) -> None:
        """Point every view at the saved rows from the chosen days."""
        self.posts = days.keep(self._saved["posts"], self.chosen_days)
        self.comments = days.keep(self._saved["comments"], self.chosen_days)
        self.posts_tab.load(self.posts, animate)
        self.comments_tab.load(self.comments, animate)
        self._update_dataset_labels()
        self._render_overview()

    def _on_days_chosen(self, chosen: set[int] | None) -> None:
        if not self.current:
            return
        self.chosen_days = days.save_choice(self.settings, self.current, chosen, self._day_counts)
        if self.thread is not None and self._showing_fetch():
            # A running fetch has added rows since the subreddit was loaded: read them all again.
            self._flush_rows()
            self._show_dataset(self.current)
            self._read_to = {k: max([self._since] + [r["fetched_at"] for r in self._saved[k]])
                             for k in ("posts", "comments")}
        else:
            self._apply_days()

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
                f"Delete the {len(self._saved['posts']):,} posts and {len(self._saved['comments']):,} comments saved for "
                f"r/{self.current}? This can't be undone.") != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_subreddit(self.current)
        self.settings.remove(days.settings_key(self.current))
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
        self.settings.remove(days.SETTINGS_GROUP)
        self.statusBar().showMessage("Deleted all saved data.", 8000)
        self.current = None
        self._refresh_datasets()

    def _export(self, kind: str) -> None:
        if not self.current:
            return
        path, _ = QFileDialog.getSaveFileName(self, f"Export {kind}", f"{self.current}_{kind}.csv", "CSV files (*.csv)")
        if path:
            n = self.store.export_csv(self.current, kind, path, days=self.chosen_days)
            on = "" if self.chosen_days is None else f" from {plural(len(self.chosen_days), 'chosen day')}"
            self.statusBar().showMessage(f"Exported {n:,} {kind}{on} to {path}", 8000)

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
            "<p>Gather subreddit posts and comments and score their sentiment with a model that runs "
            "on this computer.</p>"
            "<p>Fonts: Manrope and Sora, both under the SIL Open Font License 1.1.</p>"))

    def paintEvent(self, event) -> None:
        paint_backdrop(self, over_native=self._native_backdrop is not None, mood=self.mood.state)

    def closeEvent(self, event) -> None:
        self._save_fetch_settings()
        if self.coverage is not None:
            self.coverage.close()
        self.settings.sync()  # app.main may end the process without Qt's usual clean-up
        if self.worker:
            # A fetch ending after this must not touch the closed store, so nothing it sends is handled.
            thread, worker = self.thread, self.worker
            worker.stop()
            for sig in (worker.progress, worker.finished, worker.failed, worker.saved, thread.finished):
                sig.disconnect()
            thread.quit()
            thread.wait(3000)
        self._live.stop()  # both read the store
        self._drip.stop()
        self.store.close()
        super().closeEvent(event)

    def running_threads(self) -> list[QThread]:
        """Fetch and count threads still running, such as one stuck on a slow request after the window
        closed."""
        threads = [self.thread, self.coverage and self.coverage._count_thread]
        return [t for t in threads if t is not None and t.isRunning()]
