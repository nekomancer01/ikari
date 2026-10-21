"""The Download coverage page: how much of each day's posts, and each post's comments, is saved.

Day totals come from the archive's aggregate counts, kept in the database and asked for again at most once a UTC
day; comment totals come from each post's own num_comments. Both count items later removed on Reddit, which
ikari skips, so a day or thread can stop short of 100%; once a "Download all" finds nothing more,
it's recorded as complete and shown with a check mark instead.

One bar at the top says what the ticked days download: their posts, their comments, or both. Each day also has
its own posts and comments buttons, and the day picked in the list shows its posts, each with its own button.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt, Slot
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QCheckBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QTableWidget, QVBoxLayout, QWidget,
)

from subsentiment import collect
from subsentiment.gui import theme
from subsentiment.gui.widgets import (
    Choice, ElidedLabel, ShareBar, VerticalScrollArea, label, panel, plural, section,
)
from subsentiment.gui.worker import BackgroundJob, CountWorker, StoreWorker

DAY = 86400
MAX_DAY_POSTS = collect.MAX_DAY_POSTS
days_in = collect.days_in  # cap on one day's "Download all"

NOTE = ("Totals come from Arctic Shift and include posts and comments later removed from Reddit, which ikari "
        "can't save. A day's share counts the removed posts and image posts ikari has seen as checked, and a thread can "
        "stop short of 100%. A check mark means everything the archive still has is saved. Days are UTC. Each day's "
        "count is asked for once a day; Recount asks again.")

THREADS_TIP = ("A post with no comments counts as complete, and so does one whose whole thread was downloaded "
               "before Arctic Shift counted its comments.")

WHAT = {"posts": "Posts", "comments": "Comments", "both": "Both"}  # what Download N days fetches


def day_text(day: int) -> str:
    d = datetime.fromtimestamp(day, timezone.utc)
    return f"{d:%a} {d.day} {d:%b %Y}"


def short_day(day: int) -> str:
    """A day in the list, whose year the range above it gives."""
    d = datetime.fromtimestamp(day, timezone.utc)
    return f"{d:%a} {d.day} {d:%b}"


def range_text(first: int, last: int) -> str:
    a, b = (datetime.fromtimestamp(t, timezone.utc) for t in (first, last))
    if a.date() == b.date():
        return f"{b.day} {b:%b %Y}"
    if a.year != b.year:
        return f"{a.day} {a:%b %Y} – {b.day} {b:%b %Y}"
    return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"


def today() -> int:
    """Start of the current UTC day, which the archive can't have a total for yet."""
    return int(time.time() // DAY * DAY)


def no_total(day: int, saved: int, total: int | None, complete: bool) -> bool:
    """True for a day whose archive count can't be shown: today (not over yet), or any day whose count
    is below what's already saved. Yesterday's count is already whole: on 20 Oct, r/AskReddit's 19 Oct
    read 4,517, the same as the sum of its 24 hourly counts."""
    if day >= today():
        return True
    return not complete and total is not None and total < saved


def share_text(saved: int, total: int | None, complete: bool, noun: str,
               removed: int = 0) -> tuple[str, str, float | None, str]:
    """(main text, side text, share 0..1 or None, tooltip) for one coverage cell. ``removed`` is how many
    of the archive's items ikari has seen were removed from Reddit: the archive's count includes them but
    they can't be saved, so they count as checked, and so do the image posts ikari skips."""
    if complete:
        tip = f"All {plural(saved, noun)} the archive still has are saved."
        if total is not None and total > saved:
            tip += f" Its count of {total:,} includes ones removed from Reddit."
        return f"{saved:,} saved", "✓ Complete", 1.0, tip
    if removed and total:
        checked = saved + removed
        share = min(1.0, checked / total)
        tip = (f"Arctic Shift counts {plural(total, noun)} here. ikari has checked {min(checked, total):,} of "
               f"them: {saved:,} saved and {removed:,} removed from Reddit or image posts, which aren't saved.")
        return f"{saved:,} saved", f"{100 * share:.0f}%", share, tip
    if total is None:
        return f"{saved:,} saved", "total unknown", None, "The archive hasn't counted these yet."
    if total == 0 and saved == 0:
        return f"No {noun}s", "", None, f"The archive has no {noun}s here."
    share = min(1.0, saved / total) if total else 1.0
    return f"{saved:,} of {total:,}", f"{100 * share:.0f}%", share, ""


class FitLabel(QLabel):
    """A line that shows its short form where the long one doesn't fit: a day of thousands of posts drops the
    word "threads" rather than run into the next column."""

    def __init__(self, role: str | None = "muted"):
        super().__init__()
        if role:
            theme.mark(self, "role", role)
        self.forms = ("", "")

    def set(self, full: str, short: str) -> None:
        self.forms = (full, short)
        self._fit()

    def full_text(self) -> str:
        return self.forms[0]

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self) -> QSize:
        return QSize(self.fontMetrics().horizontalAdvance(self.forms[1]), super().sizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        full, short = self.forms
        self.setText(full if self.fontMetrics().horizontalAdvance(full) <= self.width() else short)


class Cell(QWidget):
    """"12 of 340 ........ 4%" over a bar."""

    def __init__(self):
        super().__init__()
        self.lay = lay = QVBoxLayout(self)
        self._pad(open_day=False)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.main = FitLabel(None)
        self.side = label("", "muted")
        top.addWidget(self.main, 1)
        top.addWidget(self.side)
        lay.addLayout(top)
        self.bar = ShareBar()
        keep = self.bar.sizePolicy()
        keep.setRetainSizeWhenHidden(True)  # rows stay the same height with or without a bar
        self.bar.setSizePolicy(keep)
        lay.addWidget(self.bar)
        # An open day has no bar, so its side text goes on its own line there: side by side,
        # "1,234 saved so far" and "No usable count" don't fit the column at 150%.
        self.note = label("", "muted")
        self.note.setVisible(False)
        lay.addWidget(self.note)

    def _pad(self, open_day: bool) -> None:
        # The bar keeps its room when hidden, so an open day trades the padding for its second line.
        bar = self.bar.sizePolicy() if hasattr(self, "bar") else None
        if bar is not None:
            bar.setRetainSizeWhenHidden(not open_day)
            self.bar.setSizePolicy(bar)
        self.lay.setContentsMargins(*((8, 2, 12, 2) if open_day else (8, 7, 12, 9)))
        self.lay.setSpacing(0 if open_day else 5)

    def set_open(self, saved: int, why: str = "today", total: int | None = None) -> None:
        """A day without a usable total: just what's saved so far, no share or estimate. ``why`` is
        "today" (not over yet) or "wrong" (a day whose count is below what's saved)."""
        self.main.set(f"{saved:,} saved so far", f"{saved:,} so far")
        self.side.setText("")
        self.note.setText("Today" if why == "today" else "No usable count")
        self.bar.setVisible(False)  # an empty bar would read as 0%
        self.note.setVisible(True)
        self._pad(open_day=True)
        self.setToolTip(
            "Today isn't over, so there's no total yet. Download all fetches everything posted today so far."
            if why == "today" else
            f"Arctic Shift counted {total or 0:,} posts for this day, fewer than are already saved, so the "
            "count can't be right. Download all fetches everything posted that day.")

    def set(self, saved: int, total: int | None, complete: bool, noun: str, removed: int = 0,
            waiting: bool = False) -> None:
        """``waiting``: the archive's count is still being asked for, which the bar shows with a throbber."""
        main, side, share, tip = share_text(saved, total, complete, noun, removed)
        if waiting:
            side, tip = "counting…", "Asking Arctic Shift how many posts this day has."
        self.show_share(main, main, side, share, complete, tip, waiting)

    def show_share(self, main: str, short: str, side: str, share: float | None, complete: bool, tip: str = "",
                   waiting: bool = False) -> None:
        """Any share: ``short`` is the main text where the long one doesn't fit."""
        self.bar.setVisible(True)
        self.note.setVisible(False)
        self._pad(open_day=False)
        self.main.set(main, short)
        self.side.setText(side)
        self.bar.set(share, complete, waiting=waiting)
        self.setToolTip(tip if tip else (f"{main}, {side}" if side else main))
        self._fit()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        """In a narrow column the right-hand note ("total unknown", "40%") steps aside rather than being cut
        off; the bar and the tooltip still say it."""
        m = self.layout().contentsMargins()
        room = self.width() - m.left() - m.right()
        need = self.main.sizeHint().width() + 8 + self.side.sizeHint().width()
        self.side.setVisible(bool(self.side.text()) and need <= room)


def small_button(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setStyleSheet("padding: 5px 12px;")
    # Downloads start from a click only: no Enter or Space on a button that kept focus after a click.
    b.setAutoDefault(False)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


DOWNLOAD, CHECK = "↓", "↻"  # a row's buttons: download what's missing, or look for what came since


def row_button() -> QPushButton:
    """A row's download (↓) or check again (↻) button; its tooltip says what it fetches."""
    b = small_button(DOWNLOAD)
    b.setStyleSheet("padding: 3px 0; border-radius: 9px;")
    b.setFixedWidth(34)
    return b


def table(headers: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setShowGrid(False)
    t.setWordWrap(False)
    t.setFrameShape(QFrame.Shape.NoFrame)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(50)
    t.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    t.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
    h = t.horizontalHeader()
    h.setHighlightSections(False)
    h.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
    return t


def centred(w: QWidget) -> QWidget:
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(2, 0, 6, 0)
    lay.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
    return box


class CoveragePage(VerticalScrollArea):
    """A page in the rail. It lists the days in the Fetch drawer's range, so a change there shows here; the
    running fetch's progress and Stop are in the strip under the header, as on every page. On a short window it
    scrolls, as the Overview does, rather than holding the window taller than the screen."""

    def __init__(self, window):
        super().__init__()
        self.window_ = window
        settings = window.settings
        self.settings = settings
        self.subreddit: str | None = None
        self.busy = False
        self._waiting: set[int] = set()  # days whose Download waits for the archive's count of them
        self.days: list[int] = []
        self.totals: dict[str, dict[int, int]] = {}  # subreddit (lowercase) -> day -> archive count
        self._totals_day = today()  # the UTC day self.totals were asked on: a count is good for that day only
        self.count_error = ""
        self.count_job = BackgroundJob(self)
        self.count_job.ended.connect(self._on_count_done)
        self._counting: tuple[str, float, float] = ("", 0.0, 0.0)  # what the running count asked for
        self._count_stopped = False  # the app closed while it ran, so its answer may be cut short
        self._post_rows: list = []
        self._post_ids: list[str] = []
        self._built: dict[int, tuple] = {}  # post rows with widgets, and what they last showed
        self._comments_done: set[str] = set()
        self._comments_so_far: set[str] = set()
        self._day_rows: list[int] = []
        # Day -> (saved, archive count or None, removed and image posts seen, complete), as last shown.
        self._day_state: dict[int, tuple] = {}
        # Day -> (posts with every comment saved, saved posts), for self._threads_for: (subreddit lowercased, after,
        # before). Counted on a thread, as counting a month's comments takes a busy subreddit's ~650k about 70 ms.
        self._threads: dict[int, tuple[int, int]] = {}
        self._threads_for: tuple = ()
        self.threads_job = BackgroundJob(self)
        self.threads_job.ended.connect(self._on_threads_ended)
        self._threads_got: tuple = ((), None)  # what the running count asked for, and its answer once in
        self._threads_again = False  # asked for while one ran: counted again when it ends
        self.ticked: set[int] = set()  # days ticked for Download N days or Delete N days
        self.delete_job = BackgroundJob(self)  # deletes ticked days on a connection of its own
        self.delete_job.ended.connect(self._on_delete_ended)
        self._deleting: tuple = ("", (), None)  # (subreddit, days, (posts, comments) deleted or an error)
        self._anchor: int | None = None  # the day last ticked or unticked, where a shift-click range starts
        # The download of ticked days this page started last: (request, subreddit, what, days). One that stops
        # part way is kept in the settings, so Resume can start it again after a restart too.
        self._run: tuple | None = None
        try:
            self._stopped: dict | None = json.loads(settings.value("coverage/stopped", "", str) or "null")
        except ValueError:
            self._stopped = None

        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        caption = label("Download coverage", "caption")
        caption.setToolTip(NOTE)
        titles.addWidget(caption)
        self.subtitle = ElidedLabel("", "subtitle")
        self.subtitle.setToolTip(NOTE)
        titles.addWidget(self.subtitle)
        # A download of ticked days that stopped part way, offered again.
        self.resume_bar = theme.mark(QFrame(), "choice", True)
        lay = QHBoxLayout(self.resume_bar)
        lay.setContentsMargins(12, 3, 3, 3)
        lay.setSpacing(8)
        self.resume_text = QLabel()
        lay.addWidget(self.resume_text)
        self.resume = theme.mark(small_button("Resume"), "primary", True)
        self.resume.setStyleSheet("padding: 3px 12px; border-radius: 10px;")
        self.resume.clicked.connect(self._resume)
        lay.addWidget(self.resume)
        self.recount = mark_flat(small_button("Recount"))
        self.recount.setToolTip("Ask Arctic Shift for each day's post count again")
        self.recount.clicked.connect(self._recount)
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addLayout(titles, 1)
        header.addWidget(self.resume_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        header.addWidget(self.recount, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        # What the ticked days download, and the one button that does it.
        bar, bar_lay = panel("solid", margins=8, spacing=10)
        bar_lay.setDirection(QBoxLayout.Direction.LeftToRight)
        bar_lay.setContentsMargins(14, 7, 7, 7)
        bar_lay.addWidget(QLabel("Download"))
        self.what = Choice(list(WHAT.items()), settings.value("coverage/what", "both", str))
        self.what.buttons["posts"].setToolTip("Every post from the ticked days")
        self.what.buttons["comments"].setToolTip("The comments on the ticked days' saved posts, where a thread "
                                                 "isn't saved whole")
        self.what.buttons["both"].setToolTip("Every post from the ticked days, then every comment on them")
        self.what.changed.connect(lambda key: (settings.setValue("coverage/what", key), self._show_ticked()))
        bar_lay.addWidget(self.what)
        self.estimate = ElidedLabel("", "muted")
        bar_lay.addWidget(self.estimate, 1)
        self.download_ticked = theme.mark(small_button("Download selected"), "primary", True)
        self.download_ticked.clicked.connect(self._download_ticked)
        bar_lay.addWidget(self.download_ticked)
        root.addWidget(bar)

        self.day_table = table(["Day", "Posts", "", "Comment threads", ""])
        h = self.day_table.horizontalHeader()
        for col, size in ((0, 142), (2, 44), (4, 44)):  # a tick box, then the date; the two row buttons
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            h.resizeSection(col, size)
        for col in (1, 3):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.day_table.itemSelectionChanged.connect(self._load_posts)
        self.filter = Choice([("all", "All"), ("posts", "Missing posts"), ("comments", "Missing comments")], "all",
                             small=True)
        self.filter.buttons["posts"].setToolTip("Only days with posts the archive has and ikari hasn't saved")
        self.filter.buttons["comments"].setToolTip("Only days with saved posts whose comments aren't all saved")
        self.filter.changed.connect(lambda _: (self._apply_filter(), self._show_ticked()))
        # Ticked days download together, one after another under one progress bar.
        self.tick_all = mark_flat(small_button("Select all"))
        self.tick_all.clicked.connect(self._tick_all)
        self.tick_all.setText("Select none")
        self.tick_all.setMinimumWidth(self.tick_all.sizeHint().width())  # doesn't shift as its text changes
        left, left_lay = panel("solid", margins=14, spacing=8)
        days_top = QHBoxLayout()
        days_top.setSpacing(10)
        days_top.addWidget(section("Days"), 1)
        days_top.addWidget(self.tick_all)
        left_lay.addLayout(days_top)
        # The filter and the posts' order on the second row of each panel, so the two tables start level.
        left_lay.addWidget(self.filter, 0, Qt.AlignmentFlag.AlignLeft)
        left_lay.addWidget(self.day_table, 1)
        # Under the table, away from the downloads: what's saved from the ticked days can be deleted, so they
        # download again from scratch. It asks first, right here: the question names what goes, over its two
        # buttons.
        self.delete_ticked = mark_flat(small_button("Delete selected"))
        self.delete_ticked.clicked.connect(lambda: self._ask_delete(True))
        self.delete_question = label("", "muted")
        self.delete_question.setWordWrap(True)
        self.delete_cancel = mark_flat(small_button("Cancel"))
        self.delete_cancel.clicked.connect(lambda: self._ask_delete(False))
        self.delete_confirm = small_button("Delete")
        self.delete_confirm.clicked.connect(self._delete_ticked)
        self._asking = False  # the question is showing
        for w in (self.delete_question, self.delete_cancel, self.delete_confirm):
            w.setVisible(False)
        left_lay.addWidget(self.delete_question)
        days_foot = QHBoxLayout()
        days_foot.setSpacing(10)
        # The days' comment threads added up: the ticked ones, or every day listed when none are.
        self.threads_total = ElidedLabel("", "muted")
        days_foot.addWidget(self.threads_total, 1)
        for b in (self.delete_ticked, self.delete_cancel, self.delete_confirm):
            days_foot.addWidget(b)
        left_lay.addLayout(days_foot)

        self.post_table = table(["Post", "Comments saved", ""])
        h = self.post_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(1, 170)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(2, 44)
        self.post_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.post_table.verticalScrollBar().valueChanged.connect(self._fill_visible)
        self._post_viewport = self.post_table.viewport()
        self._post_viewport.installEventFilter(self)  # resized or shown: more rows may be on screen
        self.posts_title = ElidedLabel("Posts", "section")
        self.all_comments = small_button("Download all comments")
        self.all_comments.clicked.connect(self._download_day_comments)
        self.order = Choice([("missing", "Missing first"), ("newest", "Newest"), ("comments", "Most comments")],
                            settings.value("coverage/post_order", "missing", str), small=True)
        self.order.changed.connect(lambda key: (settings.setValue("coverage/post_order", key), self._load_posts()))
        self.posts_empty = label("", "muted")
        self.posts_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.posts_empty.setWordWrap(True)
        right, right_lay = panel("solid", margins=14, spacing=8)
        top = QHBoxLayout()
        top.addWidget(self.posts_title, 1)
        top.addWidget(self.all_comments)
        right_lay.addLayout(top)
        right_lay.addWidget(self.order, 0, Qt.AlignmentFlag.AlignLeft)
        # Room for each table's stretching columns ("13 saved", a post title) even in the narrowest window.
        day_room = 2 * (self.day_table.fontMetrics().horizontalAdvance("9,999 so far") + 28)  # plus the cells' margins
        for t, fixed, stretch in ((self.day_table, (0, 2, 4), day_room), (self.post_table, (1, 2), 120)):
            h = t.horizontalHeader()
            t.setMinimumWidth(sum(h.sectionSize(i) for i in fixed) + stretch + 12)
        # Both panels' title rows as tall as the button, so the two tables start level with the filters.
        days_top.itemAt(0).widget().setMinimumHeight(self.all_comments.sizeHint().height())
        right_lay.addWidget(self.post_table, 1)
        right_lay.addWidget(self.posts_empty, 1)

        self.body = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.body.setSpacing(14)
        self.body.addWidget(left, 7)
        self.body.addWidget(right, 5)
        self.panels = (left, right)
        root.addLayout(self.body, 1)

        # What the archive counts are doing: asking, or why they failed. One line's room even when empty, so
        # the tables don't shift when it says something.
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(self.status.fontMetrics().lineSpacing())
        root.addWidget(self.status)
        self.setWidget(inner)
        self._show_resume()

    # -- what's shown ---------------------------------------------------------

    def set_subreddit(self, name: str | None) -> None:
        if (name or "").lower() != (self.subreddit or "").lower():
            self.subreddit = name
            self.day_table.clearSelection()
            self.ticked.clear()
            self._anchor = None
            self._asking = False
            self.count_error = ""
        self._show_resume()
        if self.isVisible():
            self.refresh_saved(rebuild=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_saved(rebuild=True)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self._show_resume()
        self.refresh_saved()

    def refresh_saved(self, rebuild: bool = False) -> None:
        """Re-read what's saved (a fetch may have added some) and redraw both lists. Only while the page
        shows: showing it does this anyway."""
        if not self.isVisible():
            return
        if not self.subreddit:
            self.subtitle.setText("Pick a saved subreddit first.")
            self.days = self._day_rows = []
            self.day_table.setRowCount(0)
            self._show_ticked()
            self._show_posts(None)
            return
        after, before = self.window_.fetch_form.bounds()
        days = days_in(after, before)
        if days != self.days:
            self.days, rebuild = days, True
        # The heading above names the subreddit.
        self.subtitle.setText(f"{range_text(days[-1], days[0])} · {plural(len(days), 'day')} · the dates set in Fetch")
        totals = self._totals()
        missing = [d for d in days if d not in totals and d < today()]  # today has no usable count yet
        if missing and not self.count_job.busy and not self.count_error:
            self._count(min(missing), max(missing) + DAY)
        self._fill_days(totals, rebuild)
        self._load_posts()

    def _totals(self) -> dict[int, int]:
        """The listed days' archive counts asked for today, in this session or kept from an earlier one: a count
        is asked for at most once a UTC day."""
        if self._totals_day != today():  # past midnight UTC: yesterday's counts are asked for again
            self.totals, self._totals_day, self.count_error = {}, today(), ""
        totals = self.totals.setdefault(self.subreddit.lower(), {})
        missing = [d for d in self.days if d not in totals and d < today()]
        if missing:
            totals.update(self.window_.store.archive_counts(self.subreddit, min(missing), max(missing) + DAY,
                                                            since=today()))
        return totals

    def _fill_days(self, totals: dict[int, int], rebuild: bool) -> None:
        store = self.window_.store
        saved = store.posts_per_day(self.subreddit, self.days[-1], self.days[0] + DAY)
        removed = store.removed_per_day(self.subreddit, self.days[-1], self.days[0] + DAY)
        for day, n in store.posts_per_day(self.subreddit, self.days[-1], self.days[0] + DAY, images=True).items():
            removed[day] = removed.get(day, 0) + n  # skipped image posts are checked too
        complete = store.complete_keys("posts", self.subreddit)
        self._count_threads()
        t = self.day_table
        if rebuild or self._day_rows != self.days:
            keep = self._selected_day()
            t.setRowCount(0)
            t.setRowCount(len(self.days))
            self.ticked &= set(self.days)
            for row, day in enumerate(self.days):
                tick = QCheckBox()
                tick.setFocusPolicy(Qt.FocusPolicy.NoFocus)
                tick.setCursor(Qt.CursorShape.PointingHandCursor)
                tick.setToolTip("Tick to download with the other ticked days. Shift-click ticks a run of days.")
                tick.setChecked(day in self.ticked)
                tick.clicked.connect(lambda on, d=day: self._tick(d, on))
                box = QWidget()
                lay = QHBoxLayout(box)
                lay.setContentsMargins(10, 0, 0, 0)
                lay.setSpacing(8)
                lay.addWidget(tick)
                lay.addWidget(QLabel(short_day(day)), 1)  # a click on the date still picks the row
                t.setCellWidget(row, 0, box)
                t.setCellWidget(row, 1, Cell())
                posts = row_button()
                posts.clicked.connect(lambda _=False, d=day: self._download_day(d))
                t.setCellWidget(row, 2, centred(posts))
                t.setCellWidget(row, 3, Cell())
                threads = row_button()
                threads.clicked.connect(lambda _=False, d=day: self._download_day_threads(d))
                t.setCellWidget(row, 4, centred(threads))
            self._day_rows = list(self.days)
            row = self.days.index(keep) if keep in self.days else 0
            t.selectRow(row)
        counting = self.count_job.busy
        current = today()
        self._waiting = set()
        for row, day in enumerate(self.days):
            n, total, done = saved.get(day, 0), totals.get(day), str(day) in complete
            gone = removed.get(day, 0)
            self._day_state[day] = (n, total, gone, done)
            cell: Cell = t.cellWidget(row, 1)
            button: QPushButton = t.cellWidget(row, 2).findChild(QPushButton)
            if no_total(day, n, total, done):
                cell.set_open(n, "today" if day >= current else "wrong", total)
                button.setText(DOWNLOAD)
                button.setEnabled(not self.busy)
                button.setToolTip("Download all: every post from today so far" if day >= current
                                  else f"Download all: every post from {day_text(day)}")
                continue
            # Until its count is in, a day can't be told apart from one that's done or empty, so it waits;
            # a complete day needs no count to be checked again.
            waiting = total is None and counting and not done
            if waiting:
                self._waiting.add(day)
            cell.set(n, total, done, "post", gone, waiting)
            full = done or (total is not None and 0 < total <= n + gone)
            button.setText(CHECK if full else DOWNLOAD)
            button.setEnabled(not self.busy and not waiting
                              and not (total == 0 and not done and day < self.days[0]))
            button.setToolTip(
                "Waiting for Arctic Shift's count of this day" if waiting else
                "Check again: look for posts the archive has added since" if full else
                f"Download all: every post from {day_text(day)}"
                + (f" (up to {total - n - gone:,} more)" if total and total > n + gone else ""))
        self._show_threads()
        self._apply_filter()
        self._show_ticked()

    def _missing_posts(self, day: int) -> bool:
        """The day has posts to download: no count says it's all saved."""
        n, total, gone, done = self._day_state.get(day, (0, None, 0, False))
        return not done and (total is None or n + gone < total)

    def _missing_threads(self, day: int) -> int:
        whole, posts = self._threads_on(day)
        return posts - whole

    def _apply_filter(self) -> None:
        """Hide the days the filter above the list leaves out."""
        key = self.filter.value()
        for row, day in enumerate(self._day_rows):
            hide = (key == "posts" and not self._missing_posts(day)) or (key == "comments"
                                                                          and not self._missing_threads(day))
            self.day_table.setRowHidden(row, hide)

    def _shown_days(self) -> list[int]:
        return [d for r, d in enumerate(self._day_rows) if not self.day_table.isRowHidden(r)]

    # -- comment threads saved whole ------------------------------------------

    def _count_threads(self) -> None:
        """Count the listed days' whole comment threads again (a download may have added some)."""
        if self.threads_job.busy:
            self._threads_again = True
            return
        self._threads_again = False
        want = (self.subreddit.lower(), self.days[-1], self.days[0] + DAY)
        worker = StoreWorker(self.window_.db_path, "whole_threads_per_day", *want)
        worker.finished.connect(self._on_threads)  # a failed count leaves what's shown, until the next one
        self._threads_got = (want, None)
        self.threads_job.start(worker)

    @Slot(object)
    def _on_threads(self, counts: dict) -> None:
        self._threads_got = (self._threads_got[0], counts)

    @Slot()
    def _on_threads_ended(self) -> None:
        asked, counts = self._threads_got
        if counts is not None:
            self._threads, self._threads_for = counts, asked
        if not (self.subreddit and self.days and self.isVisible()):
            return  # left meanwhile; showing the page again counts them
        if self._threads_again or asked != (self.subreddit.lower(), self.days[-1], self.days[0] + DAY):
            self._count_threads()
        self._show_threads()
        self._apply_filter()
        self._show_ticked()

    def _threads_on(self, day: int) -> tuple[int, int]:
        """(posts with every comment saved, saved posts) on a listed day, as last counted; (0, 0) until then."""
        current = self._threads_for[:1] == ((self.subreddit or "").lower(),)
        return self._threads.get(day, (0, 0)) if current else (0, 0)

    def _show_threads(self) -> None:
        """Each day's comment threads column: how many of its saved posts have every comment saved."""
        for row, day in enumerate(self._day_rows):
            cell: Cell = self.day_table.cellWidget(row, 3)
            button: QPushButton = self.day_table.cellWidget(row, 4).findChild(QPushButton)
            whole, posts = self._threads_on(day)
            if not posts:
                cell.show_share("No posts saved", "None", "", None, False,
                                "Download the day's posts first; their comments come after.")
                button.setText(DOWNLOAD)
                button.setEnabled(False)
                button.setToolTip("No posts from this day are saved yet")
                continue
            done = whole == posts
            cell.show_share(f"✓ {plural(posts, 'thread')}" if done else f"{whole:,} of {plural(posts, 'thread')}",
                            f"✓ {posts:,}" if done else f"{whole:,} of {posts:,}",
                            "" if done else f"{100 * whole // posts}%", whole / posts, done,
                            f"Every comment is saved on {whole:,} of the {plural(posts, 'post')} saved from this "
                            "day. " + THREADS_TIP)
            button.setText(CHECK if done else DOWNLOAD)
            button.setEnabled(not self.busy)
            button.setToolTip(f"Check again: look for comments added since on {day_text(day)}'s posts" if done else
                              f"Download the comments on the {plural(posts - whole, 'post')} from {day_text(day)} "
                              "missing some")

    # -- ticked days ----------------------------------------------------------

    def _tick(self, day: int, on: bool) -> None:
        """A tick box clicked; with Shift held, every day shown from the last one clicked to this one follows it."""
        days = [day]
        shown = self._shown_days()
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier and self._anchor in shown:
            a, b = sorted((shown.index(self._anchor), shown.index(day)))
            days = shown[a:b + 1]
        (self.ticked.update if on else self.ticked.difference_update)(days)
        self._anchor = day
        self._show_ticked()

    def _tick_all(self) -> None:
        shown = set(self._shown_days())
        self.ticked = set() if shown and self.ticked >= shown else shown
        self._show_ticked()

    def untick(self, days) -> None:
        """Days a download read to the end: ticked no longer."""
        self.ticked.difference_update(days)
        self._show_ticked()

    def _todo(self, days) -> tuple[int, int]:
        """(posts the archive has beyond what's saved, threads not saved whole) on these days, as far as known."""
        posts = 0
        for d in days:
            n, total, gone, done = self._day_state.get(d, (0, None, 0, False))
            if total is not None and not done and d < today():
                posts += max(0, total - n - gone)
        return posts, sum(self._missing_threads(d) for d in days)

    def _show_ticked(self, *_) -> None:
        """Tick boxes, the download bar and the buttons under the list, from self.ticked."""
        t = self.day_table
        for row, day in enumerate(self._day_rows):
            t.cellWidget(row, 0).findChild(QCheckBox).setChecked(day in self.ticked)
        n = len(self.ticked)
        shown = set(self._shown_days())
        every = bool(shown) and self.ticked >= shown
        self.tick_all.setText("Select none" if every else "Select all")
        self.tick_all.setEnabled(bool(shown))
        what = self.what.value()
        self.download_ticked.setText(f"Download {plural(n, 'day')}" if n else "Download selected")
        idle = not self.busy and not self.delete_job.busy
        # Comments need no count; posts wait for theirs, as a day's Download all does.
        counting = self.ticked & self._waiting if what != "comments" else set()
        posts, threads = self._todo(self.ticked)
        nothing = what == "comments" and not threads
        self.download_ticked.setEnabled(bool(n) and idle and not counting and not nothing)
        self.download_ticked.setToolTip(
            "Tick days in the list to download them together" if not n else
            "Waiting for Arctic Shift's count of the ticked days" if counting else
            "Every comment thread on the ticked days is saved whole" if nothing else
            {"posts": "Download every post from the ticked days, newest first",
             "comments": "Download the comments on the ticked days' saved posts, where a thread isn't saved whole",
             "both": "Download every post from the ticked days, newest first, then every comment on them"}[what])
        self.estimate.setText("" if not n else {
            "posts": f"≈ {plural(posts, 'post')} to fetch" if posts else "Checks for posts added since",
            "comments": f"{plural(threads, 'thread')} to fetch",
            "both": f"≈ {plural(posts, 'post')} and their comments, plus {plural(threads, 'saved thread')}"}[what])
        self.delete_ticked.setText(f"Delete {plural(n, 'day')}…" if n else "Delete selected")
        self.delete_ticked.setEnabled(bool(n) and idle and bool(self.subreddit))
        self.delete_ticked.setToolTip("Delete the posts and comments saved from the ticked days, so they show as "
                                      "not downloaded" if n else "Tick days to delete what's saved from them")
        self._asking = self._asking and bool(n) and idle and bool(self.subreddit)
        if self._asking:  # the ticks can change while it asks: it always names what the button deletes
            days = sorted(self.ticked)
            saved = self.window_.store.posts_per_day(self.subreddit, days[0], days[-1] + DAY)
            count = sum(saved.get(d, 0) for d in days)
            when = day_text(days[0]) if n == 1 else f"{range_text(days[0], days[-1])} ({plural(n, 'day')})"
            self.delete_question.setText(
                f"Delete {plural(count, 'post')} saved from {when} in r/{self.subreddit}, with their comments and "
                "sentiment? The days show as not downloaded, ready to download again. Timeline notes stay. "
                "This can't be undone.")
            self.delete_confirm.setText(f"Delete {plural(n, 'day')}")
        days = sorted(self.ticked) or self._day_rows
        whole, total = (sum(self._threads_on(d)[i] for d in days) for i in (0, 1))
        where = "the ticked days" if self.ticked else f"these {plural(len(days), 'day')}"
        self.threads_total.setText(f"Comments complete on {whole:,} of {plural(total, 'post')}" if total else "")
        self.threads_total.setToolTip(f"Every comment is saved on {whole:,} of the {plural(total, 'post')} saved "
                                      f"from {where}. " + THREADS_TIP if total else "")
        self.delete_question.setVisible(self._asking)
        self.delete_cancel.setVisible(self._asking)
        self.delete_confirm.setVisible(self._asking)
        self.delete_ticked.setVisible(not self._asking)

    def _selected_day(self) -> int | None:
        rows = self.day_table.selectionModel().selectedRows() if self.day_table.selectionModel() else []
        if rows and rows[0].row() < len(self._day_rows):
            return self._day_rows[rows[0].row()]
        return None

    def _load_posts(self) -> None:
        self._show_posts(self._selected_day())

    def _empty(self, text: str) -> None:
        # Two lines' room: in the narrowest window the hint wraps, and the squeezed panel would cut its second line.
        self.posts_empty.setText(text)
        self.posts_empty.setMinimumHeight(self.posts_empty.fontMetrics().lineSpacing() * 2 + 4)

    def _show_posts(self, day: int | None) -> None:
        t = self.post_table
        if day is None or not self.subreddit:
            self.posts_title.setText("Posts")
            t.setRowCount(0)
            self._post_ids, self._post_rows, self._built = [], [], {}
            self.all_comments.setEnabled(False)
            self._empty("Pick a day to see its posts.")
            self.posts_empty.setVisible(True)
            t.setVisible(False)
            return
        store = self.window_.store
        rows = store.comment_coverage(self.subreddit, day, day + DAY)  # newest first
        order = self.order.value()
        if order == "missing":  # threads not saved whole first, each part newest first
            rows.sort(key=lambda r: bool(r["whole"]))
        elif order == "comments":
            rows.sort(key=lambda r: -max(r["num_comments"] or 0, r["saved_comments"]))
        complete = store.complete_keys("comments", self.subreddit)
        so_far = store.complete_keys("comments_so_far", self.subreddit)
        self.posts_title.setText(f"Posts on {day_text(day)} · {len(rows):,}")
        ids = [r["id"] for r in rows]
        if ids != self._post_ids:
            # A busy day has thousands of posts, and three widgets a row took seconds to build; rows get
            # their widgets when they scroll into view (_fill_visible), so a new post costs the same as any.
            keep = t.verticalScrollBar().value() if set(ids) >= set(self._post_ids) else 0
            t.setRowCount(0)
            t.setRowCount(len(rows))
            self._post_ids, self._built = ids, {}
            t.verticalScrollBar().setValue(keep)
        self._post_rows = rows
        self._comments_done, self._comments_so_far = complete, so_far
        # Not offered again: threads saved whole, or saved whole before the archive counted them (until it
        # has, there's nothing to say more came; once it has, a count above what's saved brings them back).
        todo = [r["id"] for r in rows if not r["whole"]]
        self._fill_visible()
        self._todo_comments = todo
        self.all_comments.setText(f"Download all comments ({len(todo):,} posts)" if todo else "Download all comments")
        self.all_comments.setEnabled(not self.busy and bool(todo))
        self._empty("No posts from this day are saved yet. Use the day's ↓ in the Posts column.")
        self.posts_empty.setVisible(not rows)
        t.setVisible(bool(rows))

    def _fill_visible(self, *_) -> None:
        """Build and fill the post rows on screen, plus a screenful either side for smooth scrolling."""
        t = self.post_table
        n = len(self._post_rows)
        if not n:
            return
        height = max(t.viewport().height(), 1)
        first = t.rowAt(0)
        last = t.rowAt(height - 1)
        first = 0 if first < 0 else first
        last = n - 1 if last < 0 else last
        span = last - first + 1
        for i in range(max(0, first - span), min(n, last + span + 1)):
            self._fill_post_row(i)

    def _fill_post_row(self, i: int) -> None:
        t, r = self.post_table, self._post_rows[i]
        done = r["id"] in self._comments_done
        so_far = r["num_comments"] is None and r["id"] in self._comments_so_far
        state = (r["id"], r["title"], r["saved_comments"], r["num_comments"], done, so_far, self.busy)
        if self._built.get(i) == state:
            return
        if i not in self._built:
            title = ElidedLabel()
            title.setContentsMargins(8, 0, 4, 0)
            t.setCellWidget(i, 0, title)
            t.setCellWidget(i, 1, Cell())
            button = row_button()
            button.clicked.connect(lambda _=False, row=i: self._download_comments([self._post_rows[row]["id"]]))
            t.setCellWidget(i, 2, centred(button))
        self._built[i] = state
        total = r["num_comments"]
        t.cellWidget(i, 0).setText(" ".join((r["title"] or "").split()))
        cell: Cell = t.cellWidget(i, 1)
        cell.set(r["saved_comments"], total, done, "comment")
        if so_far and not done:
            cell.side.setText("all so far")
            cell.bar.set(1.0, False)  # full, but not the green of a thread that can't grow any more
            cell.setToolTip("Every comment the archive had on this post when it was downloaded is saved. Arctic "
                            "Shift counts a post's comments when it re-checks the post, usually about 36 hours "
                            "after posting; Check again fetches any that came since.")
        elif total is None and not done:
            cell.side.setText("not counted yet")
            cell.setToolTip("Arctic Shift counts a post's comments when it re-checks the post, usually "
                            "about 36 hours after posting. Downloading still fetches what's there now.")
        full = done or so_far or (bool(total) and r["saved_comments"] >= total)
        button: QPushButton = t.cellWidget(i, 2).findChild(QPushButton)
        button.setText(CHECK if full else DOWNLOAD)
        button.setToolTip("Check again: look for comments added since" if full else "Download all of its comments")
        button.setEnabled(not self.busy and (total != 0 or done))

    # -- layout ---------------------------------------------------------------

    def minimumSizeHint(self) -> QSize:
        """As narrow as the wider panel: where the two don't fit side by side (1280x800 at 125%), Posts go
        under Days rather than holding the window wider than the screen."""
        hint = super().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        hint.setWidth(min(hint.width(), max(p.minimumSizeHint().width() for p in self.panels) + bar))
        return hint

    def resizeEvent(self, event) -> None:
        left, right = self.panels
        need = left.minimumSizeHint().width() + self.body.spacing() + right.minimumSizeHint().width()
        wide = self.viewport().width() >= need
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self.body.direction() != direction:
            self.body.setDirection(direction)
        super().resizeEvent(event)

    def eventFilter(self, obj, event) -> bool:
        if (obj is self._post_viewport and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)
                and shiboken6.isValid(self.post_table)):  # not while the window is being torn down
            self._fill_visible()
        return super().eventFilter(obj, event)

    # -- archive counts -------------------------------------------------------

    def _count(self, after: float, before: float) -> None:
        sub = self.subreddit
        self.status.setText(f"Asking Arctic Shift how many posts r/{sub} has each day…")
        worker = CountWorker(sub, after, before)
        # Bound methods, not lambdas, so the results are handled on the GUI thread.
        worker.finished.connect(self._on_counts)
        worker.failed.connect(self._on_count_failed)
        self._counting = (sub.lower(), after, before)
        self._count_stopped = False
        self.count_job.start(worker)

    @Slot(str, object)
    def _on_counts(self, sub: str, counts: dict) -> None:
        if self._count_stopped:
            return  # the app is closing, and the answer may be cut short
        self.totals.setdefault(sub.lower(), {}).update(counts)
        # Kept, so they aren't asked for again today, here or after a restart. Today's own isn't: it's still growing.
        self.window_.store.save_archive_counts(sub, {d: n for d, n in counts.items() if d < today()})
        if not self._count_is_current():
            return  # for a subreddit no longer shown: not this one's error
        self.status.setText("")
        if not counts:
            self.count_error = "Arctic Shift couldn't count these days."
            self.status.setText(self.count_error + " Press Recount to try again.")

    def _count_is_current(self) -> bool:
        return not self._count_stopped and self._counting[0] == (self.subreddit or "").lower()

    @Slot(str)
    def _on_count_failed(self, message: str) -> None:
        if not self._count_is_current():
            return
        self.count_error = message
        self.status.setText(f"Couldn't get totals from Arctic Shift: {message} Press Recount to try again.")

    @Slot()
    def _on_count_done(self) -> None:
        if not (self.subreddit and self.days and self.isVisible()):
            return  # left meanwhile (or the app closed, and the store too); showing it again counts what's missing
        sub, after, before = self._counting
        if self._count_stopped:  # stopped while it ran: its answer may be cut short
            self.refresh_saved()
            return
        totals = self.totals.get(self.subreddit.lower(), {})
        if sub != self.subreddit.lower() or any(d not in totals and d < today() and not after <= d < before
                                                for d in self.days):
            # The user picked another subreddit or range while this count ran: count that now. It used to
            # be taken for days that couldn't be counted, so they showed "total unknown" with no reason.
            self.refresh_saved()
            return
        if any(d not in totals and d < today() for d in self.days) and not self.count_error:
            # some days timed out; don't ask again in a loop
            self.count_error = "Some days couldn't be counted."
        self._fill_days(totals, rebuild=False)

    def _recount(self) -> None:
        """Ask for every listed day's count again, even ones already asked for today."""
        if self.subreddit and self.days and not self.count_job.busy:
            self.totals.pop(self.subreddit.lower(), None)
            self.count_error = ""
            past = [d for d in self.days if d < today()]
            if past:
                self._count(past[-1], past[0] + DAY)
            self.refresh_saved()

    # -- downloads ------------------------------------------------------------

    def _download_day(self, day: int) -> None:
        if not self.subreddit:
            return
        # Everything there is, not the archive's count less what's saved: the count can lag or be off, and
        # the day is only marked complete when every part of it was read to its end.
        # Posts only: its comments have their own button, beside it.
        req = collect.FetchRequest(subreddit=self.subreddit, after=day, before=day + DAY,
                                   limit=MAX_DAY_POSTS, include_comments=False, everything=True)
        what = "so far today" if day >= today() else f"from {day_text(day)}"
        self.window_.start_request(req, f"Downloading every post {what} in r/{self.subreddit}…")

    def _download_day_threads(self, day: int) -> None:
        """A day's comments button: the threads not saved whole, or, when every one is, all of them again."""
        if not self.subreddit:
            return
        ids = self.window_.store.open_threads(self.subreddit, [day])
        if not ids:
            ids = [r["id"] for r in self.window_.store.comment_coverage(self.subreddit, day, day + DAY)]
        self._download_comments(ids)

    def _download_ticked(self) -> None:
        if not self.subreddit or not self.ticked:
            return
        self._start(self.subreddit, self.what.value(), tuple(sorted(self.ticked, reverse=True)))

    def _start(self, sub: str, what: str, days: tuple[int, ...]) -> None:
        """Download ``what`` from these days of r/sub, newest first."""
        if what == "comments":
            ids = self.window_.store.open_threads(sub, days)
            if not ids:
                return
            req = collect.FetchRequest(subreddit=sub, comment_posts=tuple(ids))
            message = f"Downloading the comments on {plural(len(ids), 'post')} from {plural(len(days), 'day')}…"
        else:
            comments = what == "both"
            req = collect.FetchRequest(subreddit=sub, after=days[-1], before=days[0] + DAY, limit=None,
                                       include_comments=comments, comments_per_post=0, days=days)
            message = (f"Downloading every {'post and comment' if comments else 'post'} from "
                       f"{plural(len(days), 'day')} in r/{sub}…")
        # The ticks stay until it ends: unticked once it has read them all, kept (and offered to resume) if it's stopped.
        if self.window_.start_request(req, message):
            self._run = (req, sub, what, days)

    def fetch_ended(self, req: collect.FetchRequest, stopped: bool) -> None:
        """A fetch ended: if it's this page's download of ticked days, untick them, or, stopped part way, offer
        to resume it."""
        if self._run is None or self._run[0] is not req:
            return
        _, sub, what, days = self._run
        self._run = None
        if stopped:
            self._stopped = {"subreddit": sub, "what": what, "days": list(days), "at": time.time()}
        else:
            self._stopped = None
            if (self.subreddit or "").lower() == sub.lower():
                self.untick(days)
        self.settings.setValue("coverage/stopped", json.dumps(self._stopped) if self._stopped else "")
        self._show_resume()

    def _show_resume(self) -> None:
        s = self._stopped
        here = bool(s) and (self.subreddit or "").lower() == s["subreddit"].lower()
        self.resume_bar.setVisible(here)
        if not here:
            return
        days = s["days"]
        when = datetime.fromtimestamp(s["at"]).strftime("%H:%M")
        what = {"posts": "posts", "comments": "comments", "both": "posts and comments"}[s["what"]]
        self.resume_text.setText(f"Stopped at {when}: {what} from {plural(len(days), 'day')}")
        self.resume_bar.setToolTip(f"{range_text(min(days), max(days))}. Resume carries on where it got to.")
        self.resume.setEnabled(not self.busy and not self.delete_job.busy)

    def _resume(self) -> None:
        s = self._stopped
        if not s or self.busy:
            return
        days = tuple(sorted(s["days"], reverse=True))
        self.ticked = set(days) & set(self.days) if self.days else set()
        self._show_ticked()
        self._start(s["subreddit"], s["what"], days)

    def _download_comments(self, ids: list[str]) -> None:
        if not self.subreddit or not ids:
            return
        req = collect.FetchRequest(subreddit=self.subreddit, comment_posts=tuple(ids))
        self.window_.start_request(req, f"Downloading every comment on {plural(len(ids), 'post')}…")

    def _download_day_comments(self) -> None:
        self._download_comments(list(getattr(self, "_todo_comments", [])))

    # -- deleting -------------------------------------------------------------

    def _ask_delete(self, on: bool) -> None:
        """Show or take back the question Delete N days asks before it deletes anything."""
        self._asking = on
        self._show_ticked()

    def _delete_ticked(self) -> None:
        if not self._asking or not self.subreddit or not self.ticked or self.busy or self.delete_job.busy:
            return
        sub, days = self.subreddit, tuple(sorted(self.ticked))
        worker = StoreWorker(self.window_.db_path, "delete_days", sub, days)
        worker.finished.connect(self._on_deleted)
        worker.failed.connect(self._on_delete_failed)
        self._asking = False
        self._deleting = (sub, days, None)
        self.status.setText(f"Deleting {plural(len(days), 'day')} of r/{sub}…")
        self.delete_job.start(worker)
        self.window_.set_deleting(True)
        self._show_ticked()

    @Slot(object)
    def _on_deleted(self, counts: tuple) -> None:
        self._deleting = (*self._deleting[:2], counts)

    @Slot(str)
    def _on_delete_failed(self, message: str) -> None:
        self._deleting = (*self._deleting[:2], message)

    @Slot()
    def _on_delete_ended(self) -> None:
        """Its thread is done, so the saved data can be written again; every page shows what's left."""
        sub, days, got = self._deleting
        self.window_.set_deleting(False)
        if isinstance(got, tuple):
            if (self.subreddit or "").lower() == sub.lower():
                self.untick(days)
            self.status.setText("")
            self.window_.days_deleted(sub, len(days), *got)
        else:
            self.status.setText(f"Couldn't delete those days: {got}")
            self._show_ticked()

    def stop_count(self) -> None:
        """As the app closes. The archive count isn't waited for: a request still out held the app's close for
        up to 3 s. It ends by itself; app.main ends the process if it still runs when the app quits. Counting
        threads reads the file, so that is waited for, with nothing of it shown."""
        if self.count_job.busy:
            self.count_job.stop()
            self._count_stopped = True
        if self.threads_job.busy:
            self.threads_job.ended.disconnect()
            self.threads_job.qthread.quit()
            self.threads_job.qthread.wait(3000)


def mark_flat(button: QPushButton) -> QPushButton:
    return theme.mark(button, "flat", True)
