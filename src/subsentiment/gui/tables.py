"""The Posts and Comments tables: their model and the filterable page that shows one."""
from __future__ import annotations

import heapq
import time
from datetime import datetime
from itertools import islice
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QEvent, QItemSelectionModel, QModelIndex, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTableView, QVBoxLayout, QWidget,
)

from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
from subsentiment.gui.widgets import label, mark, plural
from subsentiment.gui.worker import Slices
from subsentiment.store import column

SORT_ROLE = Qt.ItemDataRole.UserRole + 1  # the value a column sorts by (the model sorts by it itself)
NUMERIC = ("sentiment", "score", "num_comments")
PENDING = ("score", "num_comments")  # None until Arctic Shift re-fetches the item, about 36 hours in
PENDING_TIP = "Arctic Shift fills this in about 36 hours after posting. Fetch this range again later with Update saved posts ticked."
HEADER_TIP = ("From the Arctic Shift archive of Reddit. These appear once the archive re-checks a post, usually "
              "36 hours after posting; a dash means not yet.")
FADE_SECONDS = 1.4  # rows that arrive during a fetch fade in over this long


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _one_line(text: str, n: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


Column = tuple[str, str, Callable[[Any], str]]

POST_COLUMNS: list[Column] = [
    ("Sentiment", "sentiment", lambda v: f"{v:+.3f}"),
    ("Label", "label", str.title),
    ("Title", "title", _one_line),
    ("Score", "score", str),
    ("Comments", "num_comments", str),
    ("Author", "author", str),
    ("Posted", "created_utc", _fmt_time),
]

COMMENT_COLUMNS: list[Column] = [
    ("Sentiment", "sentiment", lambda v: f"{v:+.3f}"),
    ("Label", "label", str.title),
    ("Comment", "body", _one_line),
    ("Score", "score", str),
    ("Post", "post_title", lambda v: _one_line(v or "", 80)),
    ("Author", "author", str),
    ("Posted", "created_utc", _fmt_time),
]


class ChipDelegate(QStyledItemDelegate):
    """Draws a cell's value in an outlined chip: a pill with a coloured dot for the Label column, a box for
    the Sentiment column. The row's background, hover, selection and rule are drawn as for any cell, and
    a row fading in fades its chip too."""

    PAD = 5  # the cell's own padding either side (the stylesheet's item padding)
    DOT, GAP, INSET = 6, 5, 6  # the pill's dot, the gap after it, and its inner padding

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self.kind = kind  # "pill" or "box"

    def _font(self, base: QFont) -> QFont:
        """The pill's text a step smaller than the table's, the box's value in semibold."""
        f = QFont(base)
        if self.kind == "pill":
            f.setPointSizeF(base.pointSizeF() - 1)
        else:
            f.setWeight(QFont.Weight.DemiBold)
        return f

    def sizeHint(self, option, index) -> QSize:
        hint = super().sizeHint(option, index)
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        inner = QFontMetrics(self._font(option.font)).horizontalAdvance(text) + 2 * self.INSET
        if self.kind == "pill":
            inner += self.DOT + self.GAP
        return QSize(2 * self.PAD + inner, hint.height())

    def paint(self, painter: QPainter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        style = opt.widget.style() if opt.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        if not text:
            return
        ink = opt.palette.color(QPalette.ColorRole.Text)  # carries a fading row's alpha
        t = theme.tokens()
        edge = QColor(*t["edge"][:3], round(t["edge"][3] * ink.alphaF()))
        font = self._font(opt.font)
        metrics = QFontMetrics(font)
        cell = QRectF(opt.rect).adjusted(self.PAD, 0, -self.PAD, 0)
        height = min(cell.height() - 8, QFontMetrics(opt.font).height() + 8)
        if self.kind == "pill":
            width = min(cell.width(), self.INSET * 2 + self.DOT + self.GAP + metrics.horizontalAdvance(text))
        else:
            width = cell.width()
        chip = QRectF(cell.left(), cell.center().y() - height / 2, width, height).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(edge, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        radius = height / 2 if self.kind == "pill" else 6
        painter.drawRoundedRect(chip, radius, radius)
        painter.setFont(font)
        if self.kind == "pill":
            dot = QColor(index.model().color(index) or ink)
            dot.setAlphaF(ink.alphaF())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dot)
            painter.drawRoundedRect(QRectF(chip.left() + self.INSET, chip.center().y() - self.DOT / 2,
                                           self.DOT, self.DOT), 1.5, 1.5)
            room = chip.adjusted(self.INSET + self.DOT + self.GAP, 0, -self.INSET + 2, 0)
            painter.setPen(ink)
            painter.drawText(room, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                             metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(room.width())))
        else:
            painter.setPen(ink)
            painter.drawText(chip, int(Qt.AlignmentFlag.AlignCenter), text)
        painter.restore()


class RowsModel(QAbstractTableModel):
    """The rows a table shows, sorted here in Python.

    Qt's QSortFilterProxyModel sorted by asking this model for each value through data(), a Python call
    per comparison: about 3.4 million of them, and 17 seconds, for a subreddit with 100,000 comments.

    From BIG rows on, sorting is worked out a STEP of rows at a time between frames (a run each, then the runs
    merged) and the rows shown in their new order at once: sorting 650,000 comments took a second and a half
    in one go."""

    BIG = 50_000  # rows from which a sort is worked out between frames
    STEP = 20_000  # rows gone through between two frames: a few milliseconds

    def __init__(self, columns: list[Column], parent=None, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.columns = columns
        self.rows: list = []
        self._ids: set[str] = set()  # the ids in self.rows
        self._colors: dict = {}
        self._clock = clock
        self._sort: tuple[int, bool] | None = None  # (column, descending) once the view has sorted
        self._keys: list = []  # each row's sort key, in step with self.rows while sorted
        self._edits = 0  # rows replaced in place so far, so a sort worked out between frames can tell
        self._resets = 0  # times other rows were put in: a sort worked out for the old ones is let go
        self._sorting = False  # a sort is being worked out: rows added meanwhile go at the end until it's done
        self._slices = Slices(self)
        self._born: dict[str, float] = {}  # row id -> when it arrived, while it is fading in
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._tick)

    def _keys_for(self, rows: list) -> list:
        """The rows' keys in the current sort, as _key_of gives them, without a call a row."""
        key = self.columns[self._sort[0]][1]
        missing = float("-inf") if key in NUMERIC or key == "created_utc" else ""
        return [missing if v is None else v for v in column(rows, key)]

    @staticmethod
    def _place(keys: list, k, descending: bool) -> int:
        """Where a row with key ``k`` goes among ``keys``: after any equal keys, as a stable sort would put it."""
        lo, hi = 0, len(keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if (k > keys[mid]) if descending else (k < keys[mid]):
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _key_of(self, column: int) -> Callable:
        key = self.columns[column][1]
        # Below every real number, so pending rows don't land among (and tie with) scores of -1.
        missing = float("-inf") if key in NUMERIC or key == "created_utc" else ""
        return lambda row: missing if (v := row[key]) is None else v

    def _sorted(self, rows: list, keys: list | None = None) -> list:
        """``rows`` in the current sort order; also sets self._keys to match. ``keys`` are the rows' sort
        keys when the caller has them: rows that are already mostly in order then sort in about one pass."""
        if self._sort is None:
            self._keys = []
            return list(rows)
        if keys is None:
            keys = self._keys_for(rows)
        order = sorted(range(len(rows)), key=keys.__getitem__, reverse=self._sort[1])
        self._keys = [keys[i] for i in order]
        return [rows[i] for i in order]

    def _ordering(self, rows: list, step: int):
        """The order the current sort puts ``rows`` in, as (their indexes in it, each row's key), worked out
        ``step`` rows at a time and yielding between: each step sorts a run, then the runs are merged a step at a
        time, as one stable sort would order them. It's the generator's return value."""
        descending, keys, runs = self._sort[1], [], []
        for a in range(0, len(rows), step):
            keys += self._keys_for(rows[a:a + step])
            runs.append(sorted(range(a, min(a + step, len(rows))), key=keys.__getitem__, reverse=descending))
            yield
        if len(runs) <= 1:
            return (runs[0] if runs else []), keys
        merged, order = heapq.merge(*runs, key=keys.__getitem__, reverse=descending), []
        while part := list(islice(merged, step // 4)):  # merging is a microsecond a row, sorting a run far less
            order += part
            yield
        return order, keys

    def _placed(self, rows: list, step: int):
        """``rows`` in the current sort order, as (the rows, their keys, their ids), worked out between frames
        (see _ordering); the generator's return value."""
        if self._sort is None:
            shown, keys = list(rows), []
        else:
            order, keys = yield from self._ordering(rows, step)
            shown, kept = [], []
            for a in range(0, len(order), step):
                part = order[a:a + step]
                shown += map(rows.__getitem__, part)
                kept += map(keys.__getitem__, part)
                yield
            keys = kept
        ids = []
        for a in range(0, len(shown), step):
            ids += column(shown[a:a + step], "id")
            yield
        return shown, keys, ids

    def sorting(self) -> bool:
        """A big table's new order is being worked out between frames."""
        return self._slices.running()

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        """Called by the view when a header is clicked. Selected rows stay selected."""
        wanted = (column, order == Qt.SortOrder.DescendingOrder)
        if wanted != self._sort:  # the view asks twice for one sortByColumn
            self._sort = wanted
            if len(self.rows) < self.BIG:
                self._slices.stop()
                self._resort()
            else:
                self._slices.start(self._resort_steps(), lambda: None)

    def _resort_steps(self):
        """Sort a big table, worked out between frames, then shown in its new order at once. Rows a fetch adds
        meanwhile go at the end, then where the new order puts them; rows replaced meanwhile start it over."""
        self._sorting = True
        try:
            while True:
                rows, n, edits, resets, sort = self.rows, len(self.rows), self._edits, self._resets, self._sort
                shown, keys, ids = yield from self._placed(rows, self.STEP)
                if self._resets != resets:  # other rows were put in, sorted as they were
                    return
                if self.rows is rows and self._edits == edits and self._sort == sort:
                    break
            key_of, descending = self._key_of(sort[0]), sort[1]
            for r in rows[n:]:  # added meanwhile
                k = key_of(r)
                i = self._place(keys, k, descending)
                shown.insert(i, r)
                keys.insert(i, k)
                ids.insert(i, r["id"])
        finally:
            self._sorting = False
        self._resort(placed=(shown, keys, ids))

    def _resort(self, keys_kept: bool = False, placed: tuple | None = None) -> None:
        """Put the rows back in sort order; with ``keys_kept``, self._keys still holds each row's key. ``placed``
        is that order already worked out: (the rows in it, their keys, their ids)."""
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        ids = [self.rows[i.row()]["id"] for i in old]
        if placed is not None:
            self.rows, self._keys, order = placed
            if len(old) <= 20:  # looked for in C, a few milliseconds each
                where = {rid: order.index(rid) for rid in ids}
            else:
                wanted = set(ids)
                where = {rid: i for i, rid in enumerate(order) if rid in wanted}
        else:
            self.rows = self._sorted(self.rows, self._keys if keys_kept else None)
            where = {r["id"]: i for i, r in enumerate(self.rows)} if old else {}
        self.changePersistentIndexList(old, [self.index(where[rid], i.column()) for rid, i in zip(ids, old)])
        self.layoutChanged.emit()

    def _restyle(self) -> None:
        self._colors = palette()

    def color(self, index: QModelIndex) -> str | None:
        """The palette colour of the label in this row (for the Label column's dot)."""
        return self._colors.get(self.rows[index.row()]["label"])

    def restyle(self) -> None:
        """Pick up a new theme's colours."""
        self._restyle()
        if self.rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))

    def set_rows(self, rows: list, animate: bool = False) -> None:
        """Show ``rows``. With ``animate``, when they are the current rows plus new ones (a fetch in
        progress), the new ones are added and fade in, and the view keeps its place."""
        if animate and self.rows and self._ids <= {r["id"] for r in rows}:
            self.update_rows(rows)
            return
        self._slices.stop()
        rows = self._sorted(rows)
        self._show(rows, self._keys, set(column(rows, "id")))

    def set_rows_steps(self, rows: list):
        """set_rows (without ``animate``) with the order worked out between frames (see _ordering), then the
        rows shown in it at once; a click on a header meanwhile starts it over, and other rows put in end it.
        Returns the shown rows' ids, in order."""
        resets = self._resets
        while True:
            sort = self._sort
            shown, keys, ids = yield from self._placed(rows, self.STEP)
            if self._resets != resets:
                return None
            if self._sort == sort:
                break
        known = set()
        for a in range(0, len(ids), self.STEP):
            known.update(ids[a:a + self.STEP])
            yield
        if self._resets != resets or self._sort != sort:  # (sorted again meanwhile: once more, from the top)
            return (yield from self.set_rows_steps(rows)) if self._resets == resets else None
        self._slices.stop()  # a header clicked meanwhile: these rows are already in its order
        self._show(shown, keys, known)
        return ids

    def _show(self, rows: list, keys: list, ids: set) -> None:
        self.beginResetModel()
        self._resets += 1
        self.rows, self._keys, self._ids = rows, keys, ids
        self._born.clear()
        self._fade.stop()
        self._restyle()
        self.endResetModel()

    def update_rows(self, rows: list) -> None:
        """Rows saved again (with new scores) replace the ones shown; rows not shown yet are added and fade
        in. The view keeps its place."""
        where = {r["id"]: i for i, r in enumerate(self.rows)} if rows else {}
        # (While a sort is worked out the keys are the old order's; it starts over, as a row was replaced.)
        key_of = self._key_of(self._sort[0]) if self._sort is not None and not self._sorting else None
        added, changed, moved = [], False, False
        for r in rows:
            i = where.get(r["id"])
            if i is None:
                added.append(r)
            elif self.rows[i] != r:
                self.rows[i] = r
                self._edits += 1
                if key_of is not None and (k := key_of(r)) != self._keys[i]:
                    self._keys[i], moved = k, True
                changed = True
        if changed:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))
        self._insert(added, resort=moved)  # an updated score can move a row; a new label alone, sorted by date, can't

    def append_rows(self, rows: list) -> int:
        """Add rows not shown yet, each fading in from now; returns how many were new."""
        added = [r for r in rows if r["id"] not in self._ids]
        if not self._colors:
            self._restyle()
        self._insert(added)
        return len(added)

    def _insert(self, added: list, resort: bool = False) -> None:
        """Put each new row where the current sort order puts it (at the end when unsorted); they fade in.
        With ``resort``, rows already shown have new keys, so the whole table is put back in order."""
        if not added:
            if resort and self._sort is not None and not self._sorting:
                self._resort(keys_kept=True)
            return
        self._ids.update(r["id"] for r in added)
        now = self._clock()
        if self._sort is None or self._sorting or resort or len(added) > 1000:  # one insert at the end, then one re-sort
            start = len(self.rows)
            self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
            self.rows.extend(added)
            if self._sort is not None:
                self._keys.extend(map(self._key_of(self._sort[0]), added))
            self.endInsertRows()
            if self._sort is not None and not self._sorting:  # (a sort being worked out places them once it's done)
                self._resort(keys_kept=True)
        else:
            descending, key_of = self._sort[1], self._key_of(self._sort[0])
            for r in added:
                k = key_of(r)
                lo = self._place(self._keys, k, descending)
                self.beginInsertRows(QModelIndex(), lo, lo)
                self.rows.insert(lo, r)
                self._keys.insert(lo, k)
                self.endInsertRows()
        self._born.update({r["id"]: now for r in added})
        self._fade.start()

    def _tick(self) -> None:
        now = self._clock()
        for rid in [rid for rid, t in self._born.items() if now - t >= FADE_SECONDS]:
            del self._born[rid]
        if self.rows:  # the view repaints only what is on screen
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1),
                                  [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole])
        if not self._born:
            self._fade.stop()

    def _fade_in(self, row: int) -> float | None:
        """0 to 1 while ``row`` is fading in, else None."""
        if not self._born:
            return None
        born = self._born.get(self.rows[row]["id"])
        if born is None:
            return None
        t = min(1.0, (self._clock() - born) / FADE_SECONDS)
        return 1 - (1 - t) ** 3  # ease out

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section][0]
        if role == Qt.ItemDataRole.ToolTipRole and self.columns[section][1] in PENDING:
            return HEADER_TIP
        # Titles stay left-aligned even over numbers: with a stylesheet, Qt elides a right-aligned
        # title when its column is sorted ("Sentim" instead of "Sentiment").
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        _, key, fmt = self.columns[index.column()]
        value = row[key]
        if role == Qt.ItemDataRole.DisplayRole:
            if value is None:
                return "–" if key in PENDING else ""
            return fmt(value)
        if role == SORT_ROLE:
            return self._key_of(index.column())(row)
        if role == Qt.ItemDataRole.ToolTipRole and key in PENDING and value is None:
            return PENDING_TIP
        if role == Qt.ItemDataRole.ToolTipRole and key in ("title", "body"):
            return (row["title"] + "\n\n" + (row["selftext"] or "")) if key == "title" else row["body"]
        if role in (Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole):
            k = self._fade_in(index.row())
            if k is None:
                return None
            if role == Qt.ItemDataRole.ForegroundRole:
                color = QColor(self._colors.get("text", "#000"))
                color.setAlphaF(0.15 + 0.85 * k)
            else:  # a soft highlight that fades out as the text fades in
                color = QColor(self._colors.get("line", "#3b82f6"))
                color.setAlphaF(0.22 * (1 - k))
            return color
        if role == Qt.ItemDataRole.TextAlignmentRole and key in NUMERIC:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def row_at(self, i: int):
        return self.rows[i]


class TablePage(QWidget):
    """Filterable, sortable table in an outlined box; double-click opens the item on Reddit.

    A big table (RowsModel.BIG rows on) is loaded and filtered a step at a time between frames, its rows shown
    once they're all worked out; until then it shows what it did, and rows a fetch saves meanwhile wait for it."""

    SHRINKS = ("author", "post_title")  # free text that can end in "…" to leave room for the main text
    OPTIONAL = ("author", "post_title", "num_comments")  # hidden in this order when there's still no room
    FIXED = ("sentiment", "created_utc")  # written in a fixed format: "+0.123", "2026-10-20 03:54"

    def __init__(self, columns, stretch_col: int):
        super().__init__()
        self.model = RowsModel(columns)
        self._all_rows: list = []
        self._ids: set[str] = set()
        self._text_key = columns[stretch_col][1]
        self._builds = 0  # counts loads and filterings, so one worked out between frames sees it's been overtaken
        self._waiting: list | None = None  # while one is: [(rows added, or None), (rows saved again, or None)], in turn
        self._last: tuple | None = None  # (label, needle, the rows they let through), for the next keystroke
        self._slices = Slices(self)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter by text…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(lambda _: self._apply_filters())
        self.label_filter = mark(QComboBox(), "pill", True)
        self.label_filter.addItems(["All labels", "Positive", "Neutral", "Negative"])
        self.label_filter.currentTextChanged.connect(lambda _: motion.crossfade(self.view))  # first: the old rows' look
        self.label_filter.currentTextChanged.connect(lambda _: self._apply_filters())
        self.count = label("", "muted")

        self.view = QTableView()
        self.view.setModel(self.model)
        self.view.setSortingEnabled(True)
        self.view.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.view.setAlternatingRowColors(True)
        self.view.setShowGrid(False)
        self.view.setMouseTracking(True)
        self.view.verticalHeader().hide()
        self.view.verticalHeader().setDefaultSectionSize(42)
        self.view.setWordWrap(False)
        self.view.setFrameShape(QFrame.Shape.NoFrame)
        self.view.viewport().installEventFilter(self)
        self.stretch_col = stretch_col
        self._fit_due = False  # the rows changed while the page was out of sight: size the columns on show
        header = self.view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(stretch_col, QHeaderView.ResizeMode.Stretch)
        header.setHighlightSections(False)
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.view.doubleClicked.connect(self._open)
        keys = [key for _, key, _ in columns]
        self._chips = [ChipDelegate("box", self.view), ChipDelegate("pill", self.view)]
        self.view.setItemDelegateForColumn(keys.index("sentiment"), self._chips[0])
        self.view.setItemDelegateForColumn(keys.index("label"), self._chips[1])

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(self.filter, 1)
        top.addWidget(self.label_filter)
        top.addWidget(self.count)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addLayout(top)
        lay.addWidget(self.view)
        for sig in (self.model.rowsInserted, self.model.rowsRemoved, self.model.modelReset):
            sig.connect(self._update_count)

    @property
    def all_rows(self) -> list:
        """Every row loaded or added, whatever the filters show."""
        return self._all_rows

    def busy(self) -> str | None:
        """What a big table is working out between frames, for the window's throbber; None for nothing."""
        if self.model.sorting():
            return "Sorting the table\u2026"
        if self._slices.running():
            return "Filtering the table\u2026"
        return None

    def load(self, rows) -> None:
        for _ in self._loading(rows, None):
            pass

    def load_steps(self, rows):
        """load, a step at a time between frames for a big table: for Slices (the window's span)."""
        yield from self._loading(rows, self.model.STEP)

    def _loading(self, rows, step: int | None):
        """load, going through ``step`` rows (or all of them) between two yields."""
        self._slices.stop()
        self._builds += 1
        builds, self._all_rows, self._last, self._ids = self._builds, rows, None, None
        self._waiting = []
        ids = set()
        for a in range(0, len(rows), step or len(rows) or 1):
            ids.update(column(rows[a:a + (step or len(rows))], "id"))
            yield
        if self._all_rows is not rows:  # loaded again meanwhile
            return
        self._ids = ids
        if builds == self._builds:
            yield from self._filtering(step, builds)
        elif not self._slices.running():  # a filtering that overtook it is done: it left the rows saved meanwhile
            self._take_waiting()

    def add_rows(self, rows) -> int:
        """Add rows a running fetch has just saved; they fade in. Returns how many were new (or are to be
        looked at, while a big table is being worked out)."""
        if self._waiting is not None:
            self._waiting.append((list(rows), None))
            return len(rows)
        new = [r for r in rows if r["id"] not in self._ids]
        self._ids.update(r["id"] for r in new)
        self._all_rows.extend(new)
        self._last = None
        self.model.append_rows(self._filtered(new, *self._wanted()))
        return len(new)

    def update_rows(self, rows) -> int:
        """Bring in rows a finished fetch saved: ones saved again (new scores) replace the rows shown, the
        rest are added and fade in. Returns how many shown rows changed."""
        if self._waiting is not None:
            self._waiting.append((None, list(rows)))
            return 0
        where = {r["id"]: i for i, r in enumerate(self._all_rows)}
        new, changed = [], []
        for r in rows:
            i = where.get(r["id"])
            if i is None:
                new.append(r)
            elif self._all_rows[i] != r:  # most were dripped in as they were saved, and are unchanged
                self._all_rows[i] = r
                changed.append(r)
        self._ids.update(r["id"] for r in new)
        self._all_rows.extend(new)
        self._last = None
        label, needle = self._wanted()
        if label == "all labels" and not needle:
            self.model.update_rows(changed + new)
        else:  # a rescored row can change its label
            self.model.set_rows(self._filtered(self._all_rows, label, needle), animate=True)
        self._update_count()
        return len(changed)

    def _wanted(self) -> tuple[str, str]:
        """The label filter lowercased, and the text filter lowercased on one line."""
        return self.label_filter.currentText().lower(), " ".join(self.filter.text().lower().split())

    def _filtered(self, rows: list, label: str, needle: str) -> list:
        """The rows the label and text filters let through: the text on one line, ignoring case."""
        if label != "all labels":
            rows = [r for r, lab in zip(rows, column(rows, "label")) if lab == label]
        if needle:
            texts = column(rows, self._text_key)
            if " " in needle:  # line breaks and runs of spaces count as one space
                rows = [r for r, t in zip(rows, texts) if t and needle in " ".join(t.lower().split())]
            else:  # which they do anyway for a single word, so it's looked for as it is
                rows = [r for r, t in zip(rows, texts) if t and needle in t.lower()]
        return rows

    def _apply_filters(self) -> None:
        if len(self._all_rows) < self.model.BIG:
            for _ in self._filtering(None):
                pass
        else:
            self._builds += 1
            if self._waiting is None:
                self._waiting = []
            self._slices.start(self._filtering(self.model.STEP, self._builds), lambda: None)

    def _filtering(self, step: int | None, builds: int | None = None):
        """Show the rows the filters let through, going through ``step`` rows (or all of them) between two yields.
        Typing on filters only the rows the last keystroke showed. Overtaken by another load or filtering, it
        ends."""
        if builds is None:
            self._builds += 1
            builds = self._builds
        label, needle = self._wanted()
        rows, last = self._all_rows, self._last
        if label == "all labels" and not needle:
            picked = rows
        else:
            if last is not None and last[0] == label and last[1] and last[1] in needle:
                rows = last[2]  # a longer needle: only rows with the shorter one can have it
            picked = []
            for a in range(0, len(rows), step or len(rows) or 1):
                picked += self._filtered(rows[a:a + (step or len(rows))], label, needle)
                yield
                if builds != self._builds:
                    return
        chosen = self.view.selectionModel().selectedRows()
        selected = {self.model.row_at(i.row())["id"] for i in chosen}
        current = self.view.currentIndex()
        current_id = self.model.row_at(current.row())["id"] if current.isValid() else None
        if step is None or step >= len(picked):
            self.model.set_rows(picked)
            order = list(column(self.model.rows, "id")) if current_id or selected else []
        else:
            order = yield from self.model.set_rows_steps(picked)
            if order is None or builds != self._builds:
                return
        self._last = (label, needle, picked) if needle else None
        if current_id or 0 < len(selected) <= 1000:  # rows picked before still are, if the filters show them
            wanted = selected | {current_id}
            for i in (i for i, rid in enumerate(order) if rid in wanted):
                rid = order[i]
                if rid == current_id:
                    self.view.selectionModel().setCurrentIndex(
                        self.model.index(i, current.column()), QItemSelectionModel.SelectionFlag.NoUpdate)
                if rid in selected:
                    self.view.selectionModel().select(
                        self.model.index(i, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        if not self.filter.text():  # typing a filter keeps the columns as they are
            self._fit_due = True
            if self.isVisible():  # a page out of sight sizes them when it next shows
                self._fit_columns()
        self._update_count()
        if self._ids is not None:  # (else the load underway takes them once it has the ids)
            self._take_waiting()

    def _take_waiting(self) -> None:
        waiting, self._waiting = self._waiting or [], None
        for added, again in waiting:  # rows a fetch saved meanwhile
            if added is not None:
                self.add_rows(added)
            else:
                self.update_rows(again)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fit_due:
            self._fit_columns()

    def _fit_columns(self) -> None:
        """Size each column to its values and its full header title, leaving the rest to the text column.

        Qt's own sizing leaves out room for the sort arrow, so a sorted column's title got cut short."""
        self._fit_due = False
        self.view.ensurePolished()  # the stylesheet's font and padding, before the window first shows
        header = self.view.horizontalHeader()
        font = QFont(self.view.font())  # the font the stylesheet draws headers with (8.5pt medium)
        font.setPointSizeF(8.5)
        font.setWeight(QFont.Weight.Medium)
        header.setFont(font)  # Qt elides header titles using the widget's font, not the stylesheet's
        metrics = QFontMetrics(font)
        self._widths = {}  # column -> (width for its values, narrowest that still shows its title)
        for col in range(self.model.columnCount()):
            if col != self.stretch_col:
                title = self.model.headerData(col, Qt.Orientation.Horizontal)
                least = metrics.horizontalAdvance(title) + 16 + 24 + 8  # padding, sort arrow, slack
                self._widths[col] = (max(self._values_width(col), least), least)
        self._text_width = self._values_width(self.stretch_col)
        self._share_width()

    def _values_width(self, col: int) -> int:
        """How wide the column's widest value draws. Qt's sizeHintForColumn measured up to 1,000 rows
        through Python calls per column; this measures one cell for the padding (and the label's swatch)
        and the text of the column's longest distinct values."""
        rows = self.model.rows
        if not rows:
            return 0
        _, key, fmt = self.model.columns[col]
        if key in self.FIXED:  # every value is written the same length: sample both ends of the sort order,
            values = {r[key] for r in rows[:30] + rows[-30:]}  # so both signs and many digits are measured
        else:
            # Every row up to 10,000, then an even spread of them: going through 50,000 sorted rows took 20 ms a
            # column, and a widest value that common turns up in the spread too.
            values = set(column(rows if len(rows) <= 10000 else rows[::len(rows) // 10000 + 1], key))
            if len(values) > 100:  # numbers, names, titles: the widest are among the longest written ones
                values.discard(None)
                longest = len if isinstance(next(iter(values)), str) else abs  # for numbers: the most digits
                values = heapq.nlargest(100, values, key=longest) + [None]
        # A comment runs to thousands of characters, and laying one out takes a millisecond; a column never
        # needs more room than the first 160 characters take.
        texts = [fmt(v)[:160] if v is not None else ("–" if key in PENDING else "") for v in values]
        metrics = QFontMetrics(self.view.font())
        cell = self.view.sizeHintForIndex(self.model.index(0, col)).width()
        padding = cell - metrics.horizontalAdvance(self.model.data(self.model.index(0, col)) or "")
        return padding + max(metrics.horizontalAdvance(t) for t in texts)

    def _share_width(self) -> None:
        """Keep room for the text column (the title, the comment). A comment's post title and the author
        used to take so much that the comment itself showed as "T…".

        First the free-text columns (author, post title) give up width down to their titles and their
        values end in "…"; if the text column still has less than it needs, the least needed columns are
        hidden, author first. It needs its longest text, up to a third of the width and at least 30%: short
        titles leave room for the Comments column."""
        widths = getattr(self, "_widths", None)
        if not widths:
            return
        keys = {col: self.model.columns[col][1] for col in widths}
        room = self.view.viewport().width()
        need = max(room * 0.3, min(room / 3, getattr(self, "_text_width", room / 3)))
        hidden = set()
        for key in (None, *self.OPTIONAL):
            if key is not None:
                hidden |= {col for col, k in keys.items() if k == key}
            shown = {col: wl for col, wl in widths.items() if col not in hidden}
            fixed = sum(w for col, (w, _) in shown.items() if keys[col] not in self.SHRINKS)
            loose = {col: wl for col, wl in shown.items() if keys[col] in self.SHRINKS}
            spare = sum(w - least for w, least in loose.values())
            over = fixed + sum(w for w, _ in loose.values()) - (room - need)
            give = min(1.0, over / spare) if over > 0 and spare > 0 else (0.0 if over <= 0 else 1.0)
            used = fixed + sum(round(w - (w - least) * give) for w, least in loose.values())
            if room - used >= need or key == self.OPTIONAL[-1]:
                break
        header = self.view.horizontalHeader()
        for col, (w, least) in widths.items():
            header.setSectionHidden(col, col in hidden)
            header.resizeSection(col, round(w - (w - least) * give) if keys[col] in self.SHRINKS else w)

    def eventFilter(self, obj, event) -> bool:
        # The view's viewport, not this page: the page is resized before its layout resizes the view, and a
        # scroll bar appearing narrows the viewport alone, so sharing on the page's resize used a stale width
        # and hid columns that fit.
        if event.type() == QEvent.Type.Resize:
            self._share_width()
        return False

    def _update_count(self, *args) -> None:
        self.count.setText(plural(self.model.rowCount(), "row"))

    def _open(self, index) -> None:
        row = self.model.row_at(index.row())
        QDesktopServices.openUrl(QUrl(row["permalink"]))
