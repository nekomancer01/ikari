"""The Download coverage window: how much of each day's posts, and each post's comments, is saved.

Day totals come from the archive's aggregate counts (asked once per day and kept for the session),
comment totals from each post's own num_comments. Both count items later removed on Reddit, which
ikari skips, so a day or thread can stop short of 100%; once a "Download all" finds nothing more,
it's recorded as complete and shown with a check mark instead.
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone

import shiboken6
from PySide6.QtCore import QEvent, QSize, Qt, QThread, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFrame, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton,
    QTableWidget, QVBoxLayout, QWidget,
)

from subsentiment import collect
from subsentiment.gui import theme
from subsentiment.gui.widgets import ElidedLabel, ShareBar, fit_to_screen, label, panel, plural, section
from subsentiment.gui.theme import paint_backdrop
from subsentiment.gui.worker import CountWorker

DAY = 86400
MAX_DAY_POSTS = 10000  # cap on one day's "Download all"

NOTE = ("Totals come from Arctic Shift and include posts and comments later removed from Reddit, which ikari "
        "can't save. A day's share counts the removed posts ikari has seen as checked, and a thread can stop short "
        "of 100%. A check mark means everything the archive still has is saved. Days are UTC. A day's Download all fetches its posts only; comments download only from the "
        "buttons on the right.")


def day_text(day: int) -> str:
    d = datetime.fromtimestamp(day, timezone.utc)
    return f"{d:%a} {d.day} {d:%b %Y}"


def range_text(first: int, last: int) -> str:
    a, b = (datetime.fromtimestamp(t, timezone.utc) for t in (first, last))
    if a.date() == b.date():
        return f"{b.day} {b:%b %Y}"
    if a.year != b.year:
        return f"{a.day} {a:%b %Y} – {b.day} {b:%b %Y}"
    return f"{a.day} {a:%b} – {b.day} {b:%b %Y}"


def days_in(after: float, before: float) -> list[int]:
    """UTC day starts overlapping [after, before), newest first."""
    first = int(after // DAY * DAY)
    last = int(math.ceil(before / DAY) * DAY) - DAY
    return list(range(last, first - 1, -DAY))


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
    they can't be saved, so they count as checked."""
    if complete:
        tip = f"All {plural(saved, noun)} the archive still has are saved."
        if total is not None and total > saved:
            tip += f" Its count of {total:,} includes ones removed from Reddit."
        return f"{saved:,} saved", "✓ Complete", 1.0, tip
    if removed and total:
        checked = saved + removed
        share = min(1.0, checked / total)
        tip = (f"Arctic Shift counts {plural(total, noun)} here. ikari has checked {min(checked, total):,} of "
               f"them: {saved:,} saved and {removed:,} removed from Reddit, which can't be saved.")
        return f"{saved:,} saved", f"{100 * share:.0f}%", share, tip
    if total is None:
        return f"{saved:,} saved", "total unknown", None, "The archive hasn't counted these yet."
    if total == 0 and saved == 0:
        return f"No {noun}s", "", None, f"The archive has no {noun}s here."
    share = min(1.0, saved / total) if total else 1.0
    return f"{saved:,} of {total:,}", f"{100 * share:.0f}%", share, ""


class Cell(QWidget):
    """"12 of 340 ........ 4%" over a bar."""

    def __init__(self):
        super().__init__()
        self.lay = lay = QVBoxLayout(self)
        self._pad(open_day=False)
        top = QHBoxLayout()
        top.setSpacing(8)
        self.main = QLabel()
        self.side = label("", "muted")
        top.addWidget(self.main)
        top.addStretch(1)
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
        self.main.setText(f"{saved:,} saved so far")
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

    def set(self, saved: int, total: int | None, complete: bool, noun: str, removed: int = 0) -> None:
        main, side, share, tip = share_text(saved, total, complete, noun, removed)
        self.bar.setVisible(True)
        self.note.setVisible(False)
        self._pad(open_day=False)
        self.main.setText(main)
        self.side.setText(side)
        self.bar.set(share, complete)
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
    lay.setContentsMargins(4, 0, 8, 0)
    lay.addWidget(w, 0, Qt.AlignmentFlag.AlignVCenter)
    return box


class CoverageWindow(QDialog):
    def __init__(self, window):
        super().__init__(window)
        self.window_ = window
        self.setWindowTitle("Download coverage")
        self.setModal(False)
        self.resize(fit_to_screen(QSize(1120, 720), QGuiApplication.primaryScreen()))
        self.subreddit: str | None = None
        self.busy = False
        self.days: list[int] = []
        self.totals: dict[str, dict[int, int]] = {}  # subreddit (lowercase) -> day -> archive count
        self.count_error = ""
        self._count_thread: QThread | None = None
        self._counting: tuple[str, float, float] = ("", 0.0, 0.0)  # what the running count asked for
        self._count_stopped = False  # the window closed while it ran, so its answer may be cut short
        self._count_worker: CountWorker | None = None
        self._post_rows: list = []
        self._post_ids: list[str] = []
        self._built: dict[int, tuple] = {}  # post rows with widgets, and what they last showed
        self._comments_done: set[str] = set()
        self._day_rows: list[int] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label("Download coverage", "heading"))
        self.subtitle = label("", "subtitle")
        titles.addWidget(self.subtitle)
        self.recount = mark_flat(small_button("Recount"))
        self.recount.setToolTip("Ask Arctic Shift for each day's post count again")
        self.recount.clicked.connect(self._recount)
        header = QHBoxLayout()
        header.addLayout(titles, 1)
        header.addWidget(self.recount, 0, Qt.AlignmentFlag.AlignBottom)
        root.addLayout(header)
        note = label(NOTE, "muted")
        note.setWordWrap(True)
        root.addWidget(note)

        self.day_table = table(["Day", "Posts saved", ""])
        h = self.day_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(0, 140)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(2, 136)
        self.day_table.itemSelectionChanged.connect(self._load_posts)
        left, left_lay = panel("solid", margins=14, spacing=8)
        left_lay.addWidget(section("Days"))
        left_lay.addWidget(self.day_table, 1)

        self.post_table = table(["Post", "Comments saved", ""])
        h = self.post_table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(1, 200)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        h.resizeSection(2, 136)
        self.post_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.post_table.verticalScrollBar().valueChanged.connect(self._fill_visible)
        self._post_viewport = self.post_table.viewport()
        self._post_viewport.installEventFilter(self)  # resized or shown: more rows may be on screen
        self.posts_title = section("Posts")
        self.all_comments = small_button("Download all comments")
        self.all_comments.clicked.connect(self._download_day_comments)
        self.posts_empty = label("", "muted")
        self.posts_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.posts_empty.setWordWrap(True)
        right, right_lay = panel("solid", margins=14, spacing=8)
        top = QHBoxLayout()
        top.addWidget(self.posts_title, 1)
        top.addWidget(self.all_comments)
        right_lay.addLayout(top)
        # Room for each table's stretching column ("13 saved", a post title) even in the narrowest window.
        day_room = self.day_table.fontMetrics().horizontalAdvance("9,999 saved so far") + 28  # plus the cell's margins
        for t, fixed, stretch in ((self.day_table, (0, 2), day_room), (self.post_table, (1, 2), 120)):
            h = t.horizontalHeader()
            t.setMinimumWidth(sum(h.sectionSize(i) for i in fixed) + stretch + 12)
        # Both panels' title rows as tall as the button, so the two tables start level.
        left_lay.itemAt(0).widget().setMinimumHeight(self.all_comments.sizeHint().height())
        right_lay.addWidget(self.post_table, 1)
        right_lay.addWidget(self.posts_empty, 1)

        body = QHBoxLayout()
        body.setSpacing(14)
        body.addWidget(left, 5)
        body.addWidget(right, 6)
        root.addLayout(body, 1)

        self.progress = QProgressBar()
        self.progress.setFixedWidth(160)
        self.progress.setVisible(False)
        self.status = label("", "muted")
        self.status.setWordWrap(True)
        self.stop_btn = small_button("Stop")
        self.stop_btn.setVisible(False)
        self.stop_btn.clicked.connect(self.window_._stop_fetch)
        footer = QHBoxLayout()
        footer.setSpacing(12)
        footer.addWidget(self.status, 1)
        footer.addWidget(self.progress, 0, Qt.AlignmentFlag.AlignVCenter)
        footer.addWidget(self.stop_btn)
        root.addLayout(footer)

    # -- what's shown ---------------------------------------------------------

    def set_subreddit(self, name: str | None) -> None:
        if (name or "").lower() != (self.subreddit or "").lower():
            self.subreddit = name
            self.day_table.clearSelection()
            self.count_error = ""
        if self.isVisible():
            self.refresh_saved(rebuild=True)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # The footer stays as tall as with Stop showing, so the tables don't shift when a download starts.
        self.status.setMinimumHeight(max(self.stop_btn.sizeHint().height(), self.status.fontMetrics().lineSpacing()))
        self.refresh_saved(rebuild=True)

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.stop_btn.setVisible(busy)
        self.stop_btn.setEnabled(busy and not self.window_._stopping)  # pressed already: stays off
        self.progress.setVisible(busy)
        if busy:
            self.progress.setRange(0, 0)
        if self.isVisible():
            self.refresh_saved()

    def set_progress(self, done: int, total: int, msg: str) -> None:
        if total:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        self.status.setText(msg)

    def refresh_saved(self, rebuild: bool = False) -> None:
        """Re-read what's saved (a fetch may have added some) and redraw both lists."""
        if not self.subreddit:
            self.subtitle.setText("Pick a saved subreddit first.")
            self.days = self._day_rows = []
            self.day_table.setRowCount(0)
            self._show_posts(None)
            return
        after, before = self.window_.fetch_range()
        days = days_in(after, before)
        if days != self.days:
            self.days, rebuild = days, True
        self.subtitle.setText(f"r/{self.subreddit} · {range_text(days[-1], days[0])} · "
                              f"{plural(len(days), 'day')}")
        totals = self.totals.get(self.subreddit.lower(), {})
        missing = [d for d in days if d not in totals]
        if missing and self._count_thread is None and not self.count_error:
            self._count(min(missing), max(missing) + DAY)
        self._fill_days(totals, rebuild)
        self._load_posts()

    def _fill_days(self, totals: dict[int, int], rebuild: bool) -> None:
        store = self.window_.store
        saved = store.posts_per_day(self.subreddit, self.days[-1], self.days[0] + DAY)
        removed = store.removed_per_day(self.subreddit, self.days[-1], self.days[0] + DAY)
        complete = store.complete_keys("posts", self.subreddit)
        t = self.day_table
        if rebuild or self._day_rows != self.days:
            keep = self._selected_day()
            t.setRowCount(0)
            t.setRowCount(len(self.days))
            for row, day in enumerate(self.days):
                item_label = QLabel(day_text(day))
                item_label.setContentsMargins(8, 0, 0, 0)
                t.setCellWidget(row, 0, item_label)
                t.setCellWidget(row, 1, Cell())
                button = small_button("Download all")
                button.clicked.connect(lambda _=False, d=day: self._download_day(d))
                t.setCellWidget(row, 2, centred(button))
            self._day_rows = list(self.days)
            row = self.days.index(keep) if keep in self.days else 0
            t.selectRow(row)
        counting = self._count_thread is not None
        current = today()
        for row, day in enumerate(self.days):
            n, total, done = saved.get(day, 0), totals.get(day), str(day) in complete
            cell: Cell = t.cellWidget(row, 1)
            button: QPushButton = t.cellWidget(row, 2).findChild(QPushButton)
            if no_total(day, n, total, done):
                cell.set_open(n, "today" if day >= current else "wrong", total)
                button.setText("Download all")
                button.setEnabled(not self.busy)
                button.setToolTip("Fetch everything posted today so far" if day >= current
                                  else f"Fetch every post from {day_text(day)}")
                continue
            gone = removed.get(day, 0)
            cell.set(n, total, done, "post", gone)
            if total is None and counting and not done:
                cell.side.setText("counting…")
            full = done or (total is not None and 0 < total <= n + gone)
            button.setText("Check again" if full else "Download all")
            button.setEnabled(not self.busy and not (total == 0 and not done and day < self.days[0]))
            button.setToolTip(
                "Look for posts the archive has added since" if full else
                f"Fetch every post from {day_text(day)}"
                + (f" (up to {total - n - gone:,} more)" if total and total > n + gone else ""))

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
            self.posts_title.setText("POSTS")
            t.setRowCount(0)
            self._post_ids, self._post_rows, self._built = [], [], {}
            self.all_comments.setEnabled(False)
            self._empty("Pick a day to see its posts.")
            self.posts_empty.setVisible(True)
            t.setVisible(False)
            return
        store = self.window_.store
        rows = store.comment_coverage(self.subreddit, day, day + DAY)
        complete = store.complete_keys("comments", self.subreddit)
        self.posts_title.setText(f"POSTS ON {day_text(day).upper()} · {len(rows):,}")
        ids = [r["id"] for r in rows]
        if ids != self._post_ids:
            # A busy day has thousands of posts, and three widgets a row took seconds to build; rows get
            # their widgets when they scroll into view (_fill_visible), so a new post costs the same as any.
            keep = t.verticalScrollBar().value()
            t.setRowCount(0)
            t.setRowCount(len(rows))
            self._post_ids, self._built = ids, {}
            t.verticalScrollBar().setValue(keep)
        self._post_rows = rows
        self._comments_done = complete
        todo = []
        for r in rows:
            total = r["num_comments"]
            if total != 0 and not (r["id"] in complete or (bool(total) and r["saved_comments"] >= total)):
                todo.append(r["id"])
        self._fill_visible()
        self._todo_comments = todo
        self.all_comments.setText(f"Download all comments ({len(todo):,} posts)" if todo else "Download all comments")
        self.all_comments.setEnabled(not self.busy and bool(todo))
        self._empty("No posts from this day are saved yet. Use Download all on the left.")
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
        state = (r["title"], r["saved_comments"], r["num_comments"], done, self.busy)
        if self._built.get(i) == state:
            return
        if i not in self._built:
            title = ElidedLabel()
            title.setContentsMargins(8, 0, 4, 0)
            t.setCellWidget(i, 0, title)
            t.setCellWidget(i, 1, Cell())
            button = small_button("Download all")
            button.clicked.connect(lambda _=False, pid=r["id"]: self._download_comments([pid]))
            t.setCellWidget(i, 2, centred(button))
        self._built[i] = state
        total = r["num_comments"]
        t.cellWidget(i, 0).setText(" ".join((r["title"] or "").split()))
        cell: Cell = t.cellWidget(i, 1)
        cell.set(r["saved_comments"], total, done, "comment")
        if total is None and not done:
            cell.side.setText("not counted yet")
            cell.setToolTip("Arctic Shift counts a post's comments when it re-checks the post, usually "
                            "about 36 hours after posting. Downloading still fetches what's there now.")
        full = done or (bool(total) and r["saved_comments"] >= total)
        button: QPushButton = t.cellWidget(i, 2).findChild(QPushButton)
        button.setText("Check again" if full else "Download all")
        button.setEnabled(not self.busy and (total != 0 or done))

    def eventFilter(self, obj, event) -> bool:
        if (obj is self._post_viewport and event.type() in (QEvent.Type.Resize, QEvent.Type.Show)
                and shiboken6.isValid(self.post_table)):  # not while the window is being torn down
            self._fill_visible()
        return super().eventFilter(obj, event)

    # -- archive counts -------------------------------------------------------

    def _count(self, after: float, before: float) -> None:
        sub = self.subreddit
        self.status.setText(f"Asking Arctic Shift how many posts r/{sub} has each day…")
        thread = QThread(self)
        worker = CountWorker(sub, after, before)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        # Bound methods, not lambdas, so the results are handled on the GUI thread.
        worker.finished.connect(self._on_counts)
        worker.failed.connect(self._on_count_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._on_count_done)
        self._count_thread, self._count_worker = thread, worker
        self._counting = (sub.lower(), after, before)
        self._count_stopped = False
        thread.start()

    @Slot(str, object)
    def _on_counts(self, sub: str, counts: dict) -> None:
        self.totals.setdefault(sub.lower(), {}).update(counts)
        if not self._count_is_current():
            return  # stopped, or for a subreddit no longer shown: not this one's error
        if not self.busy:
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
        self._count_thread.deleteLater()
        self._count_worker.deleteLater()
        self._count_thread = self._count_worker = None
        if not (self.subreddit and self.days and self.isVisible()):
            return  # closed meanwhile (the store may be too); showing it again counts what's missing
        sub, after, before = self._counting
        if self._count_stopped:  # closed and opened again while it ran: its answer may be cut short
            self.refresh_saved()
            return
        totals = self.totals.get(self.subreddit.lower(), {})
        if sub != self.subreddit.lower() or any(d not in totals and not after <= d < before for d in self.days):
            # The user picked another subreddit or range while this count ran: count that now. It used to
            # be taken for days that couldn't be counted, so they showed "total unknown" with no reason.
            self.refresh_saved()
            return
        if any(d not in totals for d in self.days) and not self.count_error:
            # some days timed out; don't ask again in a loop
            self.count_error = "Some days couldn't be counted."
        self._fill_days(totals, rebuild=False)

    def _recount(self) -> None:
        if self.subreddit and self._count_thread is None:
            self.totals.pop(self.subreddit.lower(), None)
            self.count_error = ""
            self.refresh_saved()

    # -- downloads ------------------------------------------------------------

    def _download_day(self, day: int) -> None:
        if not self.subreddit:
            return
        # Everything there is, not the archive's count less what's saved: the count can lag or be off, and
        # the day is only marked complete when every part of it was read to its end.
        # Posts only: comments are fetched from the posts list, one post or one day at a time, when asked.
        req = collect.FetchRequest(subreddit=self.subreddit, after=day, before=day + DAY,
                                   limit=MAX_DAY_POSTS, include_comments=False, everything=True)
        what = "so far today" if day >= today() else f"from {day_text(day)}"
        self.window_.start_request(req, f"Downloading every post {what} in r/{self.subreddit}…")

    def _download_comments(self, ids: list[str]) -> None:
        if not self.subreddit or not ids:
            return
        req = collect.FetchRequest(subreddit=self.subreddit, comment_posts=tuple(ids))
        self.window_.start_request(req, f"Downloading every comment on {plural(len(ids), 'post')}…")

    def _download_day_comments(self) -> None:
        self._download_comments(list(getattr(self, "_todo_comments", [])))

    # -- look -----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        paint_backdrop(self)

    def closeEvent(self, event) -> None:
        if self._count_worker:
            # Not waited for: a request still out held the window (and the app's own close) for up to
            # 3 s. It ends by itself; app.main ends the process if it still runs when the app quits.
            self._count_worker.stop()
            self._count_stopped = True
        super().closeEvent(event)


def mark_flat(button: QPushButton) -> QPushButton:
    return theme.mark(button, "flat", True)
