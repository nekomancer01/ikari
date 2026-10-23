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
from functools import partial
from typing import Callable
from datetime import datetime, timezone

import shiboken6
from PySide6.QtCore import QEvent, QObject, QRectF, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QBoxLayout, QCheckBox, QFrame, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QSizePolicy, QStyle, QStyleOptionButton, QStylePainter, QTableWidget, QVBoxLayout, QWidget,
)

from subsentiment import collect
from subsentiment.arctic_shift import TREE_MAX
from subsentiment.gui import motion, theme
from subsentiment.store import SAMPLED
from subsentiment.gui.widgets import (
    Choice, CommentCap, ElidedLabel, ShareBar, VerticalScrollArea, label, panel, plural, section,
)
from subsentiment.gui.worker import BackgroundJob, CountWorker, StoreRead, StoreWorker

DAY = 86400
MAX_DAY_POSTS = collect.MAX_DAY_POSTS  # cap on one day's "Download all"

NOTE = ("Totals come from Arctic Shift and include posts and comments later removed from Reddit, which ikari "
        "can't save. A day's share counts the removed posts and image posts ikari has seen as checked, and a thread can "
        "stop short of 100%. A check mark means everything the archive still has is saved. Days are UTC. Each day's "
        "count is asked for once a day; Recount asks again.")

THREADS_TIP = ("A post with no comments counts as complete, and so does one whose whole thread was downloaded "
               "before Arctic Shift counted its comments.")

WHAT = {"posts": "Posts", "comments": "Comments", "both": "Both"}  # what Download N days fetches
# A day's count is kept in the database once the archive has had this long after the day ended to take in its
# last posts (ArcticShiftClient's ingest lag is an hour); one asked for sooner is used for this session only.
COUNT_SETTLED = 2 * 3600


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
        self.note = FitLabel()  # "No count" where "No usable count" doesn't fit, as the window narrows
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
        self.note.set(*(("Today", "Today") if why == "today" else ("No usable count", "No count")))
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


def small_button(text: str, pill: str = "compact") -> QPushButton:
    """``pill`` is the theme's size: "compact", "bar" (level with the action bar's boxes) or "glyph"."""
    b = theme.mark(QPushButton(text), "pill", pill)
    # Downloads start from a click only: no Enter or Space on a button that kept focus after a click.
    b.setAutoDefault(False)
    b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


DOWNLOAD, CHECK = "↓", "↻"  # a row's buttons: download what's missing, or look for what came since


class RowButton(QPushButton):
    """A row's download (↓) or check again (↻) button; its tooltip says what it fetches. Clicked while a download
    runs, it waits its turn in the page's queue: lighter, with a wheel turning in place of its arrow (still with
    motion off), until it starts or is clicked again to leave the queue."""

    QUEUED_OPACITY = 0.55

    def __init__(self):
        super().__init__(DOWNLOAD)
        theme.mark(self, "pill", "glyph")
        self.setFixedWidth(32)
        self.setAutoDefault(False)  # downloads start from a click only, as small_button's
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.queued = False
        self.later = False  # a download is running, so a click queues this one
        self.tip = ""

    def show_as(self, text: str, tip: str, enabled: bool) -> None:
        self.setText(text)
        self.setEnabled(enabled)
        self.tip = tip
        self._show_tip()

    def mark(self, queued: bool, later: bool) -> None:
        if (queued, later) != (self.queued, self.later):
            self.queued, self.later = queued, later
            self._show_tip()
            self.update()

    def _show_tip(self) -> None:
        if self.queued:
            self.setToolTip(f"{self.tip}\nQueued: it starts once the downloads before it end. Click to take it out "
                            "of the queue.")
        else:
            self.setToolTip(self.tip + ("\nA download is running: click to queue this one after it." if self.later
                                        and self.isEnabled() else ""))

    def paintEvent(self, event) -> None:
        if not self.queued:
            super().paintEvent(event)
            return
        p = QStylePainter(self)
        p.setOpacity(self.QUEUED_OPACITY)
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        opt.text = ""
        p.drawControl(QStyle.ControlElement.CE_PushButton, opt)
        # In place of the arrow, a wheel: three quarters of a ring turning on the throbbers' shared clock, the
        # text's height across.
        d = QFontMetricsF(self.font()).capHeight() * 1.2
        ring = QRectF(-d / 2, -d / 2, d, d)
        p.translate(QRectF(self.rect()).center())
        p.rotate(360 * (motion.throb(self) or 0.0))
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(self.palette().buttonText().color(), max(1.5, d / 7))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawArc(ring, 0, 270 * 16)

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


class LazyRows(QObject):
    """A table whose rows get their widgets only as they come near the view: a row's five widgets take about
    5 ms to build, so building every one of two years of days took three seconds as the page opened, and a busy
    day has thousands of posts. ``build(row)`` gives a row its widgets and ``draw(row)`` fills them in; a row is
    drawn as it's built, and again by ``redraw``, so ``draw`` skips what it already shows. The rows on screen
    are built at once, and a screenful either side a few milliseconds a frame after, ready for scrolling."""

    NEAR_MS = 4  # building the rows near the view takes at most this much of a frame

    def __init__(self, table: QTableWidget, build, draw):
        super().__init__(table)
        self.table, self.build, self.draw = table, build, draw
        self.built: set[int] = set()
        self._near: list[int] = []  # rows near the view, nearest first, for _build_near
        self._timer = QTimer(self)
        self._timer.setInterval(16)  # once a frame
        self._timer.timeout.connect(self._build_near)
        table.verticalScrollBar().valueChanged.connect(self.fill)
        self.viewport = table.viewport()
        self.viewport.installEventFilter(self)  # resized or shown: more rows may be on screen

    def reset(self, rows: int) -> None:
        """``rows`` rows, none built yet: ``fill`` builds the ones on screen, once they're hidden or scrolled to as
        they're to be."""
        self.table.setRowCount(0)
        self.table.setRowCount(rows)
        self.built.clear()

    def fill(self, *_) -> None:
        """Build the rows on screen, and soon after the ones a screenful either side."""
        t = self.table
        n = t.rowCount()
        if not n:
            return
        first, last = t.rowAt(0), t.rowAt(max(t.viewport().height(), 1) - 1)
        first = max(first, 0)
        last = n - 1 if last < 0 else last  # the rows end above the bottom: every row shown after the first
        for row in range(first, last + 1):
            if not t.isRowHidden(row):
                self._make(row)
        # Each cell widget set asks for a layout, and the ones set while it's pending are left at the table's
        # corner, over the rows on screen, until it runs: it runs now.
        t.executeDelayedItemsLayout()
        span = last - first + 1
        self._near = [r for k in range(1, span + 1) for r in (last + k, first - k) if 0 <= r < n]
        if any(r not in self.built for r in self._near):
            self._timer.start()

    def _make(self, row: int) -> None:
        if row not in self.built:
            self.build(row)
            self.built.add(row)
            self.draw(row)

    def _build_near(self) -> None:
        end = time.perf_counter() + self.NEAR_MS / 1000
        for row in self._near:
            if row < self.table.rowCount() and not self.table.isRowHidden(row):
                self._make(row)
                if time.perf_counter() > end:
                    break  # more after the next frame
        else:
            self._timer.stop()
        self.table.executeDelayedItemsLayout()

    def redraw(self) -> None:
        for row in sorted(self.built):
            self.draw(row)

    def eventFilter(self, obj, event) -> bool:
        if (obj is self.viewport and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)
                and shiboken6.isValid(self.table)):  # not while the window is being torn down
            self.fill()
        return False


def centred(w: QWidget) -> QWidget:
    """A row button in the middle of its cell. The cell is the column less the theme's item padding, so the
    button keeps no margins of its own: they pushed its right edge out of a 44 px column."""
    box = QWidget()
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.addWidget(w, 0, Qt.AlignmentFlag.AlignCenter)
    return box


class CoveragePage(VerticalScrollArea):
    """A page in the rail. It lists the days fetches and downloads of the subreddit have asked for, saved or not,
    to find and fill what they missed (Store.tried_days); the running fetch's progress and Stop are in the strip
    under the header, as on every page. On a short window it scrolls, as the Overview does, rather than holding
    the window taller than the screen."""

    def __init__(self, window):
        super().__init__()
        self.window_ = window
        settings = window.settings
        self.settings = settings
        self.subreddit: str | None = None
        self.busy = False
        self._waiting: set[int] = set()  # days whose Download waits for the archive's count of them
        self._today = today()  # the UTC day the days list was last drawn for
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
        self._post_drawn: dict[int, tuple] = {}  # what each built post row last showed
        self._comments_done: set[str] = set()
        self._comments_so_far: set[str] = set()
        self._day_rows: list[int] = []
        self._drawn: dict[tuple, tuple] = {}  # what each day's cells show (_changed)
        # Day -> (saved, archive count or None, removed and image posts seen, complete), as last shown.
        self._day_state: dict[int, tuple] = {}
        # Day -> (posts with every comment saved, saved posts), for self._threads_for: (subreddit lowercased, after,
        # before, cap).
        self._threads: dict[int, tuple[int, int]] = {}
        self._threads_for: tuple = ()
        # What's saved is read on threads, so the page opens at once: a busy subreddit's days took 50 ms to read,
        # its comment threads 70 ms more, and a busy day's posts tens of milliseconds.
        self.days_read = StoreRead(window.db_path, "coverage_days", self)
        self.days_read.got.connect(self._on_days)
        self.posts_read = StoreRead(window.db_path, "coverage_posts", self)
        self.posts_read.got.connect(self._on_posts)
        self._rebuild = False  # the next days read in builds the rows again
        self._days_for = ""  # the subreddit (lowercased) self.days are of
        self._todo_comments: list[str] = []  # the opened day's posts whose threads aren't saved whole
        self._sampled: set[int] = set()  # listed days a sample download was through, as last read
        self.ticked: set[int] = set()  # days ticked for Download N days or Delete N days
        self.delete_job = BackgroundJob(self)  # deletes ticked days on a connection of its own
        self.delete_job.ended.connect(self._on_delete_ended)
        self._deleting: tuple = ("", (), None)  # (subreddit, days, (posts, comments) deleted or an error)
        self._anchor: int | None = None  # the day last ticked or unticked, where a shift-click range starts
        # The download of ticked days this page started last: (request, subreddit, what, days). One that stops
        # part way is kept in the settings, so Resume can start it again after a restart too.
        self._run: tuple | None = None
        # Row buttons clicked while a download ran, in order: ((subreddit lowercased, kind, day or post id), start).
        # Each starts as the one before it ends; Stop, or a download that fails, empties it.
        self._queue: list[tuple[tuple, Callable[[], object]]] = []
        try:
            stopped = json.loads(settings.value("coverage/stopped", "", str) or "null")
            self._stopped: dict | None = stopped if (
                isinstance(stopped, dict) and isinstance(stopped.get("subreddit"), str)
                and stopped.get("what") in WHAT and isinstance(stopped.get("at"), (int, float))
                and stopped.get("days") and all(isinstance(d, int) for d in stopped["days"])) else None
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
        bar, rows = panel("solid", margins=8, spacing=8)
        rows.setContentsMargins(14, 7, 7, 7)
        # One line: what, the comment cap, then the estimate and the button. A narrower window puts the estimate and
        # button on a second line, and a narrower one still the cap on its own line between them.
        self.bar_lines, self.bar_first = (QBoxLayout(QBoxLayout.Direction.LeftToRight) for _ in range(2))
        bar_lay, bar_cap, bar_end = QHBoxLayout(), QHBoxLayout(), QHBoxLayout()
        for lay in (self.bar_lines, self.bar_first, bar_lay, bar_cap, bar_end):
            lay.setSpacing(10)
        rows.addLayout(self.bar_lines)
        self.bar_lines.addLayout(self.bar_first)
        self.bar_lines.addLayout(bar_end, 1)
        self.bar_first.addLayout(bar_lay)
        self.bar_first.addLayout(bar_cap)
        self.bar_first.addStretch(1)  # the cap follows the choice, not the line's far end
        bar_lay.addWidget(QLabel("Download"))
        self.what = Choice(list(WHAT.items()), settings.value("coverage/what", "both", str))
        self.what.buttons["posts"].setToolTip("Every post from the ticked days")
        self.what.buttons["comments"].setToolTip("The comments on the ticked days' saved posts, where a thread "
                                                 "isn't saved whole")
        self.what.buttons["both"].setToolTip("Every post from the ticked days, then every comment on them")
        self.what.changed.connect(lambda key: (settings.setValue("coverage/what", key), self._show_ticked()))
        bar_lay.addWidget(self.what)
        # How many comments each thread keeps, and which: every comment download on this page follows it. A thread
        # with that many saved isn't asked about again; a bigger cap later tops it up.
        self.comment_cap = CommentCap(settings, "coverage", 0, TREE_MAX, self)
        self.comment_cap.count.setSingleStep(50)
        self.comment_cap.count.setSuffix(" per thread")
        self.comment_cap.count.setSpecialValueText("All comments")
        # Just wide enough for its widest text and the arrows, leaving the bar's room to the estimate.
        count = self.comment_cap.count
        count.setFixedWidth(count.fontMetrics().horizontalAdvance("25000 per thread") + 48)
        self.comment_cap.pick.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        for signal in (self.comment_cap.count.valueChanged, self.comment_cap.pick.currentIndexChanged):
            signal.connect(self.comment_cap.save)
            signal.connect(self._show_ticked)
        # A thread holding the cap is done at that cap, so the threads and the opened day's posts follow it.
        for signal in (self.comment_cap.count.valueChanged, self.comment_cap.pick.currentIndexChanged):
            signal.connect(lambda _: self.subreddit and self.days and self.refresh_saved())
        bar_cap.addWidget(self.comment_cap.count)
        bar_cap.addWidget(self.comment_cap.pick)
        for lay in (bar_lay, bar_cap):
            lay.addStretch(1)  # on a line of its own, each part stays at the left
        self.estimate = ElidedLabel("", "muted")
        bar_end.addWidget(self.estimate, 1)
        self.download_ticked = theme.mark(small_button("Download selected", "bar"), "primary", True)
        self.download_ticked.clicked.connect(self._download_ticked)
        bar_end.addWidget(self.download_ticked)
        root.addWidget(bar)

        self.day_table = table(["Day", "Posts", "", "Comment threads", ""])
        h = self.day_table.horizontalHeader()
        for col, size in ((0, 142), (2, 44), (4, 44)):  # a tick box, then the date; the two row buttons
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Fixed)
            h.resizeSection(col, size)
        for col in (1, 3):
            h.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)
        self.day_table.itemSelectionChanged.connect(self._load_posts)
        self.day_rows = LazyRows(self.day_table, self._build_day_row, self._draw_day_row)
        self.filter = Choice([("all", "All"), ("posts", "Missing posts"), ("comments", "Missing comments")], "all",
                             small=True)
        self.filter.buttons["posts"].setToolTip("Only days with posts the archive has and ikari hasn't saved")
        self.filter.buttons["comments"].setToolTip("Only days with saved posts whose comments aren't all saved")
        self.filter.changed.connect(self._on_filter)
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
        self.post_rows = LazyRows(self.post_table, self._build_post_row, self._draw_post_row)
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
        self.viewport().installEventFilter(self)
        inner.installEventFilter(self)
        self._show_resume()

    # -- what's shown ---------------------------------------------------------

    def set_subreddit(self, name: str | None) -> None:
        if (name or "").lower() != (self.subreddit or "").lower():
            self.subreddit = name
            # The last one's days go at once, so no button acts on them for this one while its own are read.
            self.days, self._day_rows, self._days_for = [], [], ""
            self.day_rows.reset(0)
            self._show_posts(None)
            self.ticked.clear()
            self._anchor = None
            self._asking = False
            self.count_error = ""
        self._show_resume()
        if self.isVisible():
            self.refresh_saved(rebuild=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self.refresh_saved()  # the rows built before stay: only what changed is drawn again

    def set_busy(self, busy: bool) -> None:
        """Every download and delete button follows at once; what's saved is read again after."""
        self.busy = busy
        self._show_resume()
        if self.isVisible():
            self.day_rows.redraw()
            self.post_rows.redraw()
            self.all_comments.setEnabled(not busy and bool(self._todo_comments))
            self._show_ticked()
        self.refresh_saved()
        if not busy and self._queue:
            QTimer.singleShot(0, self._next)  # once the window has finished with the download that ended

    def refresh_saved(self, rebuild: bool = False) -> None:
        """Re-read what's saved (a fetch may have added some) and redraw both lists once it's in. Only while the
        page shows: showing it does this anyway."""
        if not self.isVisible():
            return
        self._rebuild = self._rebuild or rebuild
        if self.subreddit:
            self.days_read.ask(self.subreddit, self._cap(), today())
        else:
            self._on_days((None, 0, 0), {"days": []})

    def reading(self) -> bool:
        return self.days_read.busy or self.posts_read.busy

    @Slot(object, object)
    def _on_days(self, asked: tuple, got: dict) -> None:
        sub, cap, _ = asked
        if not self.isVisible() or (sub or "").lower() != (self.subreddit or "").lower():
            return  # left, or another subreddit asked for since: what that asks for is drawn instead
        days = got["days"]
        if not days:
            self.subtitle.setText("Nothing downloaded yet. Days with posts saved are listed here, to download what a fetch missed."
                                  if self.subreddit else "Pick a saved subreddit first.")
            self.days = self._day_rows = []
            self.day_rows.reset(0)
            self._show_ticked()
            self._show_posts(None)
            return
        rebuild, self._rebuild = self._rebuild or days != self.days, False
        self.days = days
        self._threads, self._threads_for = got["threads"], (sub.lower(), days[-1], days[0] + DAY, cap)
        self._sampled = got["sampled"]
        self._days_for = sub.lower()
        totals = self._totals(got["counts"])
        self._count_missing()
        self._fill_days(totals, got, rebuild)
        self._load_posts()

    def _count_missing(self) -> None:
        """Ask the archive for the listed days' counts not in yet (today has no usable count yet)."""
        if self._days_for != (self.subreddit or "").lower():
            return  # the days listed are another subreddit's: its own are counted once they're read
        totals = self.totals.get(self._days_for, {})
        missing = [d for d in self.days if d not in totals and d < today()]
        if missing and not self.count_job.busy and not self.count_error:
            self._count(min(missing), max(missing) + DAY)

    def _totals(self, kept: dict[int, int]) -> dict[int, int]:
        """The listed days' archive counts asked for today, in this session or ``kept`` from an earlier one: a
        count is asked for at most once a UTC day."""
        if self._totals_day != today():  # past midnight UTC: yesterday's counts are asked for again
            self.totals, self._totals_day, self.count_error = {}, today(), ""
        totals = self.totals.setdefault(self.subreddit.lower(), {})
        missing = [d for d in self.days if d not in totals and d < today()]
        if missing:
            totals.update({d: n for d, n in kept.items() if min(missing) <= d <= max(missing)})
        return totals

    def _fill_days(self, totals: dict[int, int], got: dict, rebuild: bool) -> None:
        saved, removed, complete = got["saved"], got["checked"], got["complete"]
        counting = self.count_job.busy
        self._today = today()
        self._waiting = set()
        for day in self.days:
            n, total, done = saved.get(day, 0), totals.get(day), str(day) in complete
            self._day_state[day] = (n, total, removed.get(day, 0), done)
            # Until its count is in, a day can't be told apart from one that's done or empty, so it waits;
            # a complete day needs no count to be checked again.
            if not no_total(day, n, total, done) and total is None and counting and not done:
                self._waiting.add(day)
        if rebuild or self._day_rows != self.days:
            keep = self._selected_day()
            self.ticked &= set(self.days)
            self._day_rows = list(self.days)
            self._drawn.clear()
            self.day_rows.reset(len(self.days))
            self.day_table.selectRow(self.days.index(keep) if keep in self.days else 0)
        else:
            self.day_rows.redraw()
        self._apply_filter()
        self.ticked &= set(self._shown_days())
        shown = self._shown_days()
        if shown and self._selected_day() not in shown:  # the picked day went: pick the first one listed
            self.day_table.selectRow(self.days.index(shown[0]))
        # The heading above names the subreddit.
        self.subtitle.setText(f"{range_text(self.days[-1], self.days[0])} · {plural(len(self.days), 'day')} "
                              "downloaded")
        self._show_ticked()

    def _build_day_row(self, row: int) -> None:
        t, day = self.day_table, self._day_rows[row]
        tick = QCheckBox()
        tick.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tick.setCursor(Qt.CursorShape.PointingHandCursor)
        tick.setToolTip("Tick to download with the other ticked days. Shift-click ticks a run of days.")
        tick.clicked.connect(lambda on, d=day: self._tick(d, on))
        box = QWidget()
        lay = QHBoxLayout(box)
        lay.setContentsMargins(10, 0, 0, 0)
        lay.setSpacing(8)
        lay.addWidget(tick)
        lay.addWidget(QLabel(short_day(day)), 1)  # a click on the date still picks the row
        t.setCellWidget(row, 0, box)
        t.setCellWidget(row, 1, Cell())
        posts = RowButton()
        posts.clicked.connect(lambda _=False, d=day: self._ask("posts", d, self._download_day))
        t.setCellWidget(row, 2, centred(posts))
        t.setCellWidget(row, 3, Cell())
        threads = RowButton()
        threads.clicked.connect(lambda _=False, d=day: self._ask("threads", d, self._download_day_threads))
        t.setCellWidget(row, 4, centred(threads))

    def _draw_day_row(self, row: int) -> None:
        """A built day row's tick, Posts and Comment threads, from what _fill_days and the thread count found."""
        day = self._day_rows[row]
        self.day_table.cellWidget(row, 0).findChild(QCheckBox).setChecked(day in self.ticked)
        self._draw_day_posts(row, day)
        self._draw_day_threads(row, day)
        sub, queued = (self.subreddit or "").lower(), self._queued()
        for col, kind in ((2, "posts"), (4, "threads")):
            self.day_table.cellWidget(row, col).findChild(RowButton).mark((sub, kind, day) in queued, self.busy)

    def _draw_day_posts(self, row: int, day: int) -> None:
        t, current = self.day_table, self._today
        n, total, gone, done = self._day_state.get(day, (0, None, 0, False))
        opened = no_total(day, n, total, done)
        waiting = day in self._waiting
        if not self._changed(("posts", day), n, total, gone, done, waiting, self.busy, current, self.days[0]):
            return
        cell: Cell = t.cellWidget(row, 1)
        button: RowButton = t.cellWidget(row, 2).findChild(RowButton)
        # A download running doesn't turn the buttons off: a click queues theirs after it.
        idle = not self.delete_job.busy
        if opened:
            cell.set_open(n, "today" if day >= current else "wrong", total)
            button.show_as(DOWNLOAD, "Download all: every post from today so far" if day >= current
                           else f"Download all: every post from {day_text(day)}", idle)
            return
        cell.set(n, total, done, "post", gone, waiting)
        full = done or (total is not None and 0 < total <= n + gone)
        button.show_as(
            CHECK if full else DOWNLOAD,
            "Waiting for Arctic Shift's count of this day" if waiting else
            "Check again: look for posts the archive has added since" if full else
            f"Download all: every post from {day_text(day)}"
            + (f" (up to {total - n - gone:,} more)" if total and total > n + gone else ""),
            idle and not waiting and not (total == 0 and not done and day < self.days[0]))

    def _changed(self, cell, *shown) -> bool:
        """Whether a day's cell (("posts" or "threads", day)) is to show other than it does: once a second while
        a download runs, filling in every cell of two years of days again took a fifth of a second."""
        shown = (self.subreddit, *shown)
        if self._drawn.get(cell) == shown:
            return False
        self._drawn[cell] = shown
        return True

    def _missing_posts(self, day: int) -> bool:
        """The day has posts to download: no count says it's all saved."""
        n, total, gone, done = self._day_state.get(day, (0, None, 0, False))
        return not done and (total is None or n + gone < total)

    def _missing_threads(self, day: int) -> int:
        whole, posts = self._threads_on(day)
        return posts - whole

    def _on_filter(self, _key: str) -> None:
        """Only the days shown stay ticked, so Download and Delete never act on days out of sight."""
        self._apply_filter()
        self.ticked &= set(self._shown_days())
        self._show_ticked()

    def _apply_filter(self) -> None:
        """Hide the days the filter above the list leaves out."""
        key = self.filter.value()
        if key == "comments" and self._threads_for[:1] != ((self.subreddit or "").lower(),):
            key = "all"  # the whole threads aren't counted yet: nothing is hidden until they are
        for row, day in enumerate(self._day_rows):
            hide = (key == "posts" and not self._missing_posts(day)) or (
                key == "comments" and not self._missing_threads(day))
            self.day_table.setRowHidden(row, hide)
        self.day_rows.fill()  # days the filter brings back may be on screen now

    def _shown_days(self) -> list[int]:
        return [d for r, d in enumerate(self._day_rows) if not self.day_table.isRowHidden(r)]

    # -- comment threads saved whole ------------------------------------------

    def _kept(self) -> str:
        """What a thread needs saved to count as done, for the tooltips: the cap counts as done."""
        if self.comment_cap.sampling:
            return "Every comment, or its part of the day's sample, is"
        cap = self.comment_cap.count.value()
        return f"Every comment, or {cap:,} on a bigger thread, is" if cap else "Every comment is"

    def _cap(self) -> int:
        """What a thread needs to be done, for the store (thread_done): the cap, or SAMPLED."""
        return SAMPLED if self.comment_cap.sampling else self.comment_cap.count.value()

    def _threads_on(self, day: int) -> tuple[int, int]:
        """(posts with every comment saved, saved posts) on a listed day, as last counted; (0, 0) until then."""
        current = self._threads_for[:1] == ((self.subreddit or "").lower(),)
        return self._threads.get(day, (0, 0)) if current else (0, 0)

    def _draw_day_threads(self, row: int, day: int) -> None:
        whole, posts = self._threads_on(day)
        if not self._changed(("threads", day), whole, posts, self.busy, self.comment_cap.sampling,
                             day in self._sampled):
            return
        cell: Cell = self.day_table.cellWidget(row, 3)
        button: RowButton = self.day_table.cellWidget(row, 4).findChild(RowButton)
        if not posts:
            cell.show_share("No posts saved", "None", "", None, False,
                            "Download the day's posts first; their comments come after.")
            button.show_as(DOWNLOAD, "No posts from this day are saved yet", False)
            return
        done = whole == posts
        # Sampled is enough for this download, but not the green of every comment saved.
        sampled = done and self.comment_cap.sampling and day in self._sampled
        cell.show_share(f"Sampled · {plural(posts, 'thread')}" if sampled else f"✓ {plural(posts, 'thread')}"
                        if done else f"{whole:,} of {plural(posts, 'thread')}",
                        "Sampled" if sampled else f"✓ {posts:,}" if done else f"{whole:,} of {posts:,}",
                        "" if done else f"{100 * whole // posts}%", whole / posts, done and not sampled,
                        f"{self._kept()} saved on {whole:,} of the {plural(posts, 'post')} saved from this "
                        "day. " + THREADS_TIP)
        button.show_as(CHECK if done else DOWNLOAD,
                       f"Check again: look for comments added since on {day_text(day)}'s posts" if done else
                       f"Download the comments on the {plural(posts - whole, 'post')} from {day_text(day)} "
                       "missing some", not self.delete_job.busy)

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
        self.day_rows.redraw()  # the rows not built yet are ticked as they're built
        n = len(self.ticked)
        shown = set(self._shown_days())
        every = bool(shown) and self.ticked >= shown
        self.tick_all.setText("Select none" if every else "Select all")
        self.tick_all.setEnabled(bool(shown))
        what = self.what.value()
        self.comment_cap.set_enabled(what != "posts")
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
            "comments": f"A sample from {plural(threads, 'thread')}" if self.comment_cap.sampling
                        else f"{plural(threads, 'thread')} to fetch",
            "both": (f"≈ {plural(posts, 'post')} and their comments" if posts else "Checks for posts added since")
                    + (f", plus {plural(threads, 'saved thread')}" if threads else "")}[what])
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
            what = (f"{plural(count, 'post')} saved from {when} in r/{self.subreddit}, with their comments, every "
                    f"comment written on {'that day' if n == 1 else 'those days'}," if count else
                    f"the comments written on {when} in r/{self.subreddit}")
            self.delete_question.setText(
                f"Delete {what} and their sentiment? The days show as not downloaded, ready to download again. "
                "Timeline notes stay. This can't be undone.")
            self.delete_confirm.setText(f"Delete {plural(n, 'day')}")
        days = sorted(self.ticked) or self._day_rows
        whole, total = (sum(self._threads_on(d)[i] for d in days) for i in (0, 1))
        where = "the ticked days" if self.ticked else f"these {plural(len(days), 'day')}"
        done = "sampled or complete" if self.comment_cap.sampling else "complete"
        self.threads_total.setText(f"Comments {done} on {whole:,} of {plural(total, 'post')}" if total else "")
        self.threads_total.setToolTip(f"{self._kept()} saved on {whole:,} of the {plural(total, 'post')} saved "
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
        day = self._selected_day()
        if day is None or not self.subreddit:
            self._show_posts(None)
        else:
            self.posts_read.ask(self.subreddit, day, self._cap())

    @Slot(object, object)
    def _on_posts(self, asked: tuple, got: tuple) -> None:
        sub, day, _ = asked
        if sub.lower() == (self.subreddit or "").lower() and day == self._selected_day():  # not one picked since
            self._show_posts(day, *got)

    def _empty(self, text: str) -> None:
        # Two lines' room: in the narrowest window the hint wraps, and the squeezed panel would cut its second line.
        self.posts_empty.setText(text)
        self.posts_empty.setMinimumHeight(self.posts_empty.fontMetrics().lineSpacing() * 2 + 4)

    def _show_posts(self, day: int | None, rows: list | None = None, complete: set[str] = frozenset(),
                    so_far: set[str] = frozenset()) -> None:
        """The posts of ``day`` as Store.coverage_posts read them, or "Pick a day" for None."""
        t = self.post_table
        if day is None or not self.subreddit:
            self.posts_title.setText("Posts")
            self._post_ids, self._post_rows, self._post_drawn = [], [], {}
            self.post_rows.reset(0)
            self.all_comments.setEnabled(False)
            self._empty("Pick a day to see its posts.")
            self.posts_empty.setVisible(True)
            t.setVisible(False)
            return
        order = self.order.value()
        if order == "missing":  # threads not saved whole first, each part newest first
            rows.sort(key=lambda r: bool(r["whole"]))
        elif order == "comments":
            rows.sort(key=lambda r: -max(r["num_comments"] or 0, r["saved_comments"]))
        self.posts_title.setText(f"Posts on {short_day(day)} · {len(rows):,}")
        ids = [r["id"] for r in rows]
        self._post_rows = rows
        self._comments_done, self._comments_so_far = complete, so_far
        if ids != self._post_ids:
            # Rows get their widgets as they scroll into view, so a new post costs the same as any.
            keep = t.verticalScrollBar().value() if set(ids) >= set(self._post_ids) else 0
            self._post_ids, self._post_drawn = ids, {}
            self.post_rows.reset(len(rows))
            t.verticalScrollBar().setValue(keep)
            self.post_rows.fill()
        else:
            self.post_rows.redraw()
        # Not offered again: threads saved whole, or saved whole before the archive counted them (until it
        # has, there's nothing to say more came; once it has, a count above what's saved brings them back).
        todo = [r["id"] for r in rows if not r["whole"]]
        self._todo_comments = todo
        self.all_comments.setText(f"Download all comments ({len(todo):,} posts)" if todo else "Download all comments")
        self.all_comments.setEnabled(not self.busy and bool(todo))
        self._empty("No posts from this day are saved yet. Use the day's ↓ in the Posts column.")
        self.posts_empty.setVisible(not rows)
        t.setVisible(bool(rows))

    def _build_post_row(self, i: int) -> None:
        t = self.post_table
        title = ElidedLabel()
        title.setContentsMargins(8, 0, 4, 0)
        t.setCellWidget(i, 0, title)
        t.setCellWidget(i, 1, Cell())
        button = RowButton()
        button.clicked.connect(lambda _=False, row=i: self._ask(
            "comments", self._post_rows[row]["id"], lambda pid, sub: self._download_comments([pid], sub, one=True)))
        t.setCellWidget(i, 2, centred(button))

    def _draw_post_row(self, i: int) -> None:
        t, r = self.post_table, self._post_rows[i]
        done = r["id"] in self._comments_done
        so_far = r["num_comments"] is None and r["id"] in self._comments_so_far
        total = r["num_comments"]
        capped = bool(r["whole"]) and not (done or so_far or (bool(total) and r["saved_comments"] >= total))
        sampled = capped and self.comment_cap.sampling
        state = (r["id"], r["title"], r["saved_comments"], r["num_comments"], done, so_far, capped, sampled, self.busy)
        if self._post_drawn.get(i) == state:
            return
        self._post_drawn[i] = state
        t.cellWidget(i, 0).setText(" ".join((r["title"] or "").split()))
        cell: Cell = t.cellWidget(i, 1)
        cell.set(r["saved_comments"], total, done, "comment")
        if so_far and not done:
            cell.side.setText("all so far")
            cell.bar.set(1.0, False)  # full, but not the green of a thread that can't grow any more
            cell.setToolTip("Every comment the archive had on this post when it was downloaded is saved. Arctic "
                            "Shift counts a post's comments when it re-checks the post, usually about 36 hours "
                            "after posting; Check again fetches any that came since.")
        elif sampled:
            cell.side.setText("sampled")
            cell.bar.set(1.0, False)  # its part of the day's sample, not the green of a thread saved whole
            cell.setToolTip(f"{plural(r['saved_comments'], 'comment')} saved: this post's part of the day's random "
                            "sample, in proportion to its comments. Its ↓ downloads every comment.")
        elif capped:
            cap = self.comment_cap.count.value()
            cell.side.setText("at cap")
            cell.bar.set(1.0, False)  # full for this cap, but not the green of a thread saved whole
            cell.setToolTip(f"{plural(r['saved_comments'], 'comment')} saved: the {cap:,} per thread the download "
                            "above keeps. A bigger cap downloads more.")
        elif total is None and not done:
            cell.side.setText("not counted yet")
            cell.setToolTip("Arctic Shift counts a post's comments when it re-checks the post, usually "
                            "about 36 hours after posting. Downloading still fetches what's there now.")
        full = done or so_far or (capped and not sampled) or (bool(total) and r["saved_comments"] >= total)
        button: RowButton = t.cellWidget(i, 2).findChild(RowButton)
        button.show_as(CHECK if full else DOWNLOAD,
                       "Check again: look for comments added since" if full else "Download all of its comments",
                       not self.delete_job.busy and (total != 0 or done))
        button.mark(((self.subreddit or "").lower(), "comments", r["id"]) in self._queued(), self.busy)

    # -- layout ---------------------------------------------------------------

    def minimumSizeHint(self) -> QSize:
        """As narrow as the wider panel: where the two don't fit side by side (1280x800 at 125%), Posts go
        under Days rather than holding the window wider than the screen."""
        hint = super().minimumSizeHint()
        bar = self.verticalScrollBar().sizeHint().width()
        hint.setWidth(min(hint.width(), max(p.minimumSizeHint().width() for p in self.panels) + bar))
        return hint

    def resizeEvent(self, event) -> None:
        self._arrange()
        super().resizeEvent(event)

    def eventFilter(self, obj, event) -> bool:
        # What's read in on a thread can change what the panels need (a button's text grows) or narrow the
        # viewport without the page resizing (the page grows taller and a scroll bar comes): they may no longer
        # fit side by side.
        if ((obj is self.viewport() and event.type() == QEvent.Type.Resize)
                or (obj is self.widget() and event.type() == QEvent.Type.LayoutRequest)):
            self._arrange()
        return super().eventFilter(obj, event)

    def _arrange(self) -> None:
        left, right = self.panels
        need = left.minimumSizeHint().width() + self.body.spacing() + right.minimumSizeHint().width()
        wide = self.viewport().width() >= need
        direction = QBoxLayout.Direction.LeftToRight if wide else QBoxLayout.Direction.TopToBottom
        if self.body.direction() != direction:
            self.body.setDirection(direction)
        # The bar on as few lines as fit, the estimate keeping room to say something.
        what, cap = (self.bar_first.itemAt(k).layout().minimumSize().width() for k in (0, 1))
        end = self.bar_lines.itemAt(1).layout().minimumSize().width()
        room = self.viewport().width() - 21  # the bar's side margins
        across, down = QBoxLayout.Direction.LeftToRight, QBoxLayout.Direction.TopToBottom
        for lay, one in ((self.bar_lines, what + cap + end + 20 + 160 <= room), (self.bar_first, what + cap + 10 <= room)):
            if lay.direction() != (across if one else down):
                lay.setDirection(across if one else down)

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
        # Kept, so they aren't asked for again today, here or after a restart; not a day the archive may still be
        # taking posts in for, whose count could still grow.
        settled = time.time() - DAY - COUNT_SETTLED
        self.window_.store.save_archive_counts(sub, {d: n for d, n in counts.items() if d <= settled})
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
            self._count_missing()
            self.refresh_saved()
            return
        if any(d not in totals and d < today() for d in self.days) and not self.count_error:
            # some days timed out; don't ask again in a loop
            self.count_error = "Some days couldn't be counted."
        self.refresh_saved()

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

    def _ask(self, kind: str, what, start: Callable) -> None:
        """A row button clicked: ``start(what, subreddit)`` now, or while a download runs, queue it after the ones
        waiting already, or take it back out of the queue if it's there."""
        if not self.subreddit:
            return
        if not self.window_.writing():
            start(what, self.subreddit)
            return
        key = (self.subreddit.lower(), kind, what)
        keys = [k for k, _ in self._queue]
        if key in keys:
            del self._queue[keys.index(key)]
        else:
            self._queue.append((key, partial(start, what, self.subreddit)))
        self._show_queue()

    def _next(self) -> None:
        """Start the first download in the queue, or the next one when there turns out to be nothing to fetch."""
        while self._queue and not self.window_.writing():
            self._queue.pop(0)[1]()
        self._show_queue()

    def clear_queue(self) -> None:
        """Stop was pressed, or a download failed: the ones waiting don't start."""
        self._queue.clear()
        self._show_queue()

    def _queued(self) -> set[tuple]:
        return {k for k, _ in self._queue}

    def _show_queue(self) -> None:
        """Mark the buttons whose downloads are queued, and say on every one that a click queues it."""
        sub, queued = (self.subreddit or "").lower(), self._queued()
        for row in self.day_rows.built:
            day = self._day_rows[row]
            for col, kind in ((2, "posts"), (4, "threads")):
                self.day_table.cellWidget(row, col).findChild(RowButton).mark((sub, kind, day) in queued, self.busy)
        for i in self.post_rows.built:
            self.post_table.cellWidget(i, 2).findChild(RowButton).mark(
                (sub, "comments", self._post_rows[i]["id"]) in queued, self.busy)

    def _download_day(self, day: int, sub: str | None = None) -> None:
        sub = sub or self.subreddit
        if not sub:
            return
        # Everything there is, not the archive's count less what's saved: the count can lag or be off, and
        # the day is only marked complete when every part of it was read to its end.
        # Posts only: its comments have their own button, beside it.
        req = collect.FetchRequest(subreddit=sub, after=day, before=day + DAY,
                                   limit=MAX_DAY_POSTS, include_comments=False, everything=True)
        what = "so far today" if day >= today() else f"from {day_text(day)}"
        self.window_.start_request(req, f"Downloading every post {what} in r/{sub}…")

    def _download_day_threads(self, day: int, sub: str | None = None) -> None:
        """A day's comments button: the threads not saved whole, or, when every one is, all of them again. Asked
        as it starts, so a queued one takes in the posts a download before it saved."""
        sub = sub or self.subreddit
        if not sub:
            return
        store = self.window_.store
        cap = self._cap()
        self._download_comments(store.open_threads(sub, [day], cap=cap) or store.open_threads(sub, [day], every=True),
                                sub)

    def _download_ticked(self) -> None:
        if not self.subreddit or not self.ticked:
            return
        self._start(self.subreddit, self.what.value(), tuple(sorted(self.ticked, reverse=True)))

    def _start(self, sub: str, what: str, days: tuple[int, ...]) -> bool:
        """Download ``what`` from these days of r/sub, newest first; False if there's nothing to start."""
        if what == "comments":
            ids = self.window_.store.open_threads(sub, days, cap=self._cap())
            if not ids:
                return False
            req = collect.FetchRequest(subreddit=sub, comment_posts=tuple(ids), fill_threads=True,
                                       **self.comment_cap.request())
            message = f"Downloading the comments on {plural(len(ids), 'post')} from {plural(len(days), 'day')}…"
        else:
            comments = what == "both"
            req = collect.FetchRequest(subreddit=sub, after=days[-1], before=days[0] + DAY, limit=None,
                                       include_comments=comments, fill_threads=True, days=days,
                                       **self.comment_cap.request())
            message = (f"Downloading every {'post and comment' if comments else 'post'} from "
                       f"{plural(len(days), 'day')} in r/{sub}…")
        # The ticks stay until it ends: unticked once it has read them all, kept (and offered to resume) if it's stopped.
        if not self.window_.start_request(req, message):
            return False
        self._run = (req, sub, what, days)
        return True

    def fetch_ended(self, req: collect.FetchRequest, stopped: bool) -> None:
        """A fetch ended: if it's this page's download of ticked days, untick them, or, stopped part way, offer
        to resume it."""
        if self._run is None or self._run[0] is not req:
            return
        _, sub, what, days = self._run
        self._run = None
        if stopped:
            self._remember_stopped({"subreddit": sub, "what": what, "days": list(days), "at": time.time()})
            return
        s = self._stopped
        if s and s["subreddit"].lower() == sub.lower() and set(s["days"]) <= set(days) and what in (s["what"], "both"):
            self._remember_stopped(None)  # what was stopped is now read to the end
        if (self.subreddit or "").lower() == sub.lower():
            self.untick(days)

    def _remember_stopped(self, stopped: dict | None) -> None:
        self._stopped = stopped
        self.settings.setValue("coverage/stopped", json.dumps(stopped) if stopped else "")
        self._show_resume()

    def _show_resume(self) -> None:
        s = self._stopped
        here = bool(s) and (self.subreddit or "").lower() == s["subreddit"].lower()
        self.resume_bar.setVisible(here)
        if not here:
            return
        days = s["days"]
        at = datetime.fromtimestamp(s["at"])
        when = at.strftime("%H:%M" if at.date() == datetime.now().date() else "%a %d %b, %H:%M")
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
        if not self._start(s["subreddit"], s["what"], days) and s["what"] == "comments" and not self.busy:
            self._remember_stopped(None)  # its threads were finished since: nothing is left to resume

    def _download_comments(self, ids: list[str], sub: str | None = None, one: bool = False) -> None:
        """Comments on these posts of r/sub (the one shown by default), as the bar above says. ``one`` post's own
        button isn't sampled: asked for by itself, it gets every comment."""
        sub = sub or self.subreddit
        if not sub or not ids:
            return
        sampling = self.comment_cap.sampling and not one
        asked = {"comments_per_post": 0, "comment_pick": "top"} if one and self.comment_cap.sampling else \
            self.comment_cap.request()
        req = collect.FetchRequest(subreddit=sub, comment_posts=tuple(ids), fill_threads=True, **asked)
        cap = asked["comments_per_post"]
        what = "a sample of the comments" if sampling else f"up to {plural(cap, 'comment')}" if cap else "every comment"
        self.window_.start_request(req, f"Downloading {what} on {plural(len(ids), 'post')}…")

    def _download_day_comments(self) -> None:
        self._download_comments(list(self._todo_comments))

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
        up to 3 s. It ends by itself; app.main ends the process if it still runs when the app quits. The reads of
        what's saved read the file, so they're waited for, with nothing of them shown."""
        if self.count_job.busy:
            self.count_job.stop()
            self._count_stopped = True
        self.days_read.close()
        self.posts_read.close()


def mark_flat(button: QPushButton) -> QPushButton:
    return theme.mark(button, "flat", True)
