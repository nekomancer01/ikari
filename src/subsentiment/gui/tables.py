"""The Posts and Comments tables: their model and the filterable page that shows one."""
from __future__ import annotations

import heapq
import time
from datetime import datetime
from itertools import islice
from typing import Any, Callable

import numpy as np
from PySide6.QtCore import QAbstractTableModel, QEvent, QItemSelectionModel, QModelIndex, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QStyle, QStyledItemDelegate,
    QStyleOptionViewItem, QTableView, QVBoxLayout, QWidget,
)

from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
from subsentiment.gui.widgets import label, mark, plural
from subsentiment.gui.rows import LABEL_AT, RowList, Rows, RowSource
from subsentiment.gui.worker import Slices, finish, on_thread
from subsentiment.analysis import LABELS
from subsentiment.store import NO_SCORE, column

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
    """The rows a table shows: positions in a source (rows.RowSource), in the order shown. A row is read when
    it's drawn, so a table of 650,000 comments holds an array of them, not the comments.

    Qt's QSortFilterProxyModel sorted by asking this model for each value through data(), a Python call per
    comparison: about 3.4 million of them, and 17 seconds, for a subreddit with 100,000 comments. Sorting by a
    number sorts the source's array of it with numpy instead, from BIG rows on on a thread (numpy lets go of the
    interpreter lock, so frames go on: 650,000 in a tenth of a second). Sorting by text is worked out a STEP of
    rows at a time between frames (a run each, then the runs merged). Either way the rows show in their new
    order at once."""

    BIG = 50_000  # rows from which a sort is worked out away from the frames
    STEP = 20_000  # rows gone through between two frames: a few milliseconds
    SLOTTED = 5_000_000  # rows added times rows shown up to which added rows are slotted in one at a time

    def __init__(self, columns: list[Column], parent=None, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.columns = columns
        self.source: RowSource = RowList()
        self.order = np.zeros(0, np.int64)  # the source positions of the rows shown, in the order shown
        # Each shown row's sort key, in step with order once sorted: an array that ascends (a number, negated for a
        # descending sort), or a list (text) in the sort's direction.
        self._keys: np.ndarray | list | None = None
        self._shown = np.zeros(0, bool)  # by source position: in order
        self._colors: dict = {}
        self._clock = clock
        self._sort: tuple[int, bool] | None = None  # (column, descending) once the view has sorted
        self._edits = 0  # rows changed in place so far, so a sort worked out away from the frames can tell
        self._resets = 0  # times other rows were put in: a sort worked out for the old ones is let go
        self._late: list[int] = []  # rows added while a sort is worked out: placed once it's done
        self._sorting = False
        self._slices = Slices(self)
        self._born: dict[int, float] = {}  # source position -> when it arrived, while it is fading in
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._tick)

    @property
    def rows(self) -> Rows:
        """The rows shown, in order, read as they're gone through."""
        return Rows(self.source, self.order)

    def _missing(self, column: int):
        key = self.columns[column][1]
        # Below every real number, so pending rows don't land among (and tie with) scores of -1.
        return float("-inf") if key in NUMERIC or key == "created_utc" else ""

    def _key_of(self, column: int) -> Callable:
        key, missing = self.columns[column][1], self._missing(column)
        return lambda row: missing if (v := row[key]) is None else v

    def _key_at(self, source: RowSource, p: int):
        """Row ``p``'s key in the current sort, as _keys holds them."""
        column, descending = self._sort
        values = source.values(self.columns[column][1])
        if isinstance(values, np.ndarray):
            k = float(values[p])
            return -k if descending else k
        v = values[p] if values is not None else source.row(p)[self.columns[column][1]]
        return self._missing(column) if v is None else v

    def _ordering(self, source: RowSource, at: np.ndarray, step: int | None):
        """The order the current sort puts the rows at ``at`` in, as (their positions in it, their keys), worked
        out with ``step`` rows between two yields (or in one go for None): a number's array sorted on a thread, text
        a run a step and the runs merged a step at a time, as one stable sort orders them. It's the generator's
        return value."""
        if self._sort is None:
            return at, None
        column, descending = self._sort
        key = self.columns[column][1]
        if source.values(key) is None:  # text not read yet
            steps = source.read_values(key)
            if step is None:
                finish(steps)
            else:
                yield from steps
        values = source.values(key)
        if isinstance(values, np.ndarray):
            keys = values[at].astype(np.float64)
            if descending:  # (stable either way: equal keys keep their order)
                keys = -keys
            if step is None or len(at) < self.BIG:
                order = np.argsort(keys, kind="stable")
            else:
                order = yield from on_thread(lambda: np.argsort(keys, kind="stable"))
            return at[order], keys[order]
        missing = self._missing(column)
        keys = [missing if (v := values[p]) is None else v for p in at.tolist()]
        if step is None or len(at) < self.BIG:
            order = sorted(range(len(keys)), key=keys.__getitem__, reverse=descending)
        else:
            runs = []
            for a in range(0, len(keys), step):
                runs.append(sorted(range(a, min(a + step, len(keys))), key=keys.__getitem__, reverse=descending))
                yield
            merged, order = heapq.merge(*runs, key=keys.__getitem__, reverse=descending), []
            while part := list(islice(merged, step // 4)):  # merging is a microsecond a row, sorting a run far less
                order += part
                yield
        return at[np.array(order, np.int64)], [keys[i] for i in order]

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        """Called by the view when a header is clicked. Selected rows stay selected."""
        wanted = (column, order == Qt.SortOrder.DescendingOrder)
        if wanted != self._sort:  # the view asks twice for one sortByColumn
            self._sort = wanted
            if len(self.order) < self.BIG:
                self._slices.stop()
                self._resort()
            else:
                self._slices.start(self._resort_steps(), lambda: None)

    def _resort(self, kept: bool = False) -> None:
        """Put the shown rows back in sort order, here and now; with ``kept``, _keys still holds each one's key."""
        finish(self._resort_steps(kept, step=None))

    def _resort_steps(self, kept: bool = False, step: int | None = STEP):
        """Sort the shown rows (worked out away from the frames, with ``step``), then show them in their new order
        at once. Rows added meanwhile go at the end until then, then where the new order puts them; rows changed
        meanwhile start it over."""
        self._sorting = True
        try:
            while True:
                order = self.order[~np.isin(self.order, self._late)] if self._late else self.order
                source, keys, edits, resets, sort = self.source, self._keys, self._edits, self._resets, self._sort
                if sort is None:
                    placed, keys = order, None
                elif kept and keys is not None and len(keys) == len(order):
                    if isinstance(keys, np.ndarray):
                        k = keys
                        moved = (np.argsort(k, kind="stable") if step is None or len(k) < self.BIG
                                 else (yield from on_thread(lambda: np.argsort(k, kind="stable"))))
                        placed, keys = order[moved], keys[moved]
                    else:
                        moved = sorted(range(len(keys)), key=keys.__getitem__, reverse=sort[1])
                        placed, keys = order[np.array(moved, np.int64)], [keys[i] for i in moved]
                else:
                    placed, keys = yield from self._ordering(source, order, step)
                if self._resets != resets:  # other rows were put in, sorted as they were
                    return
                if self._edits == edits and self._sort == sort:
                    break
                kept = False
        finally:
            self._sorting = False
        late, self._late = self._late, []
        if late and sort is not None:  # added meanwhile: where the new order puts them
            placed, keys = self._merged(placed, keys, late, [self._key_at(source, p) for p in late])
        elif late:
            placed = np.concatenate([placed, np.array(late, np.int64)])
        self._relayout(placed, keys)

    def _relayout(self, order: np.ndarray, keys) -> None:
        """Show the rows shown in the order ``order`` (as many: a layout change can't add or take away rows);
        picked rows stay picked."""
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        was = [int(self.order[i.row()]) for i in old]
        self.order, self._keys = order, keys
        self._shown = np.zeros(len(self.source), bool)
        self._shown[order] = True
        if len(old) <= 20:  # looked for in C, a millisecond each
            where = {p: (int(hit[0]) if len(hit := np.flatnonzero(order == p)) else None) for p in was}
        else:
            back = np.full(len(self.source), -1, np.int64)
            back[order] = np.arange(len(order))
            where = {p: (None if back[p] < 0 else int(back[p])) for p in was}
        self.changePersistentIndexList(old, [QModelIndex() if where[p] is None else self.index(where[p], i.column())
                                             for p, i in zip(was, old)])
        self.layoutChanged.emit()

    def _restyle(self) -> None:
        self._colors = palette()

    def color(self, index: QModelIndex) -> str | None:
        """The palette colour of the label in this row (for the Label column's dot)."""
        return self._colors.get(self.row_at(index.row())["label"])

    def restyle(self) -> None:
        """Pick up a new theme's colours."""
        self._restyle()
        if len(self.order):
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.order) - 1, len(self.columns) - 1))

    def show(self, source: RowSource, at) -> None:
        """Show the rows at positions ``at`` of ``source``, in the current sort order."""
        self._slices.stop()
        order, keys = finish(self._ordering(source, np.asarray(at, np.int64), None))
        self._show(source, order, keys)

    def show_steps(self, source: RowSource, at):
        """show, with the order worked out away from the frames (see _ordering), then the rows shown in it at once;
        a click on a header meanwhile starts it over, and other rows put in end it. Returns the shown rows'
        positions, in order (None when overtaken)."""
        at, resets = np.asarray(at, np.int64), self._resets
        while True:
            sort = self._sort
            order, keys = yield from self._ordering(source, at, self.STEP)
            if self._resets != resets:
                return None
            if self._sort == sort:
                break
        self._slices.stop()  # a header clicked meanwhile: these rows are already in its order
        self._show(source, order, keys)
        return order

    def _show(self, source: RowSource, order: np.ndarray, keys) -> None:
        self.beginResetModel()
        self._resets += 1
        self.source, self.order, self._keys, self._late = source, order, keys, []
        self._shown = np.zeros(len(source), bool)
        self._shown[order] = True
        self._born.clear()
        self._fade.stop()
        self._restyle()
        self.endResetModel()

    def set_rows(self, rows: list, animate: bool = False) -> None:
        """Show ``rows`` (a list). With ``animate``, when they are the current rows plus new ones (a fetch in
        progress), the new ones are added and fade in, and the view keeps its place."""
        if animate and len(self.order) and isinstance(self.source, RowList):
            ids = {r["id"] for r in rows}
            if all(self.source.ids[p] in ids for p in self.order.tolist()):
                self.update_rows(rows)
                return
        source = RowList(rows)
        self.show(source, np.arange(len(source)))

    def update_rows(self, rows: list) -> None:
        """Rows (a list) saved again replace the ones shown; rows not shown yet are added and fade in. The view
        keeps its place."""
        at = self._take(rows)
        shown = [p for p in at if self._is_shown(p)]
        self.refresh(shown)
        self.insert([p for p in at if not self._is_shown(p)])

    def append_rows(self, rows: list) -> int:
        """Add rows (a list) not shown yet, each fading in from now; returns how many were new."""
        return self.insert(self._take(rows))

    def _take(self, rows: list) -> list[int]:
        """The positions of ``rows`` in a list's source, added to it (or replacing what it had) first."""
        if isinstance(self.source, RowList):
            self.source.add(rows)
        return [int(p) for p in self.source.where([r["id"] for r in rows]) if p >= 0]

    def _is_shown(self, p: int) -> bool:
        return p < len(self._shown) and bool(self._shown[p])

    def insert(self, positions, fade: bool = True) -> int:
        """Show the rows at these positions too (ones shown already are skipped), each where the current sort
        puts it (at the end when unsorted); they fade in. Returns how many were new."""
        added = [p for p in dict.fromkeys(int(p) for p in positions) if not self._is_shown(p)]
        if not added:
            return 0
        if len(self._shown) < len(self.source):
            self._shown = np.concatenate([self._shown, np.zeros(len(self.source) - len(self._shown), bool)])
        self._shown[added] = True
        if not self._colors:
            self._restyle()
        if self._sort is None or self._sorting:  # at the end (a sort being worked out places them once it's done)
            start = len(self.order)
            self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
            self.order = np.concatenate([self.order, np.array(added, np.int64)])
            if self._sorting:
                self._late += added
            self.endInsertRows()
        else:
            keys = [self._key_at(self.source, p) for p in added]
            if len(added) * len(self.order) <= self.SLOTTED:  # each slotted in where it goes, the view kept as it is
                for p, k in zip(added, keys):
                    i = self._place(k)
                    self.beginInsertRows(QModelIndex(), i, i)
                    self._put([i], [p], [k])
                    self.endInsertRows()
            else:  # added at the end in one go, then moved to where they go
                order, old_keys = self.order, self._keys
                start = len(order)
                self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
                self.order = np.concatenate([order, np.array(added, np.int64)])
                self.endInsertRows()
                self._relayout(*self._merged(order, old_keys, added, keys))
        if fade:
            now = self._clock()
            self._born.update({p: now for p in added})
            self._fade.start()
        return len(added)

    def _put(self, where: list[int], added: list[int], keys: list) -> None:
        """Insert rows ``added`` before rows ``where`` of the order (as numpy.insert puts them), with their keys."""
        self.order, self._keys = self._inserted(self.order, self._keys, where, added, keys)

    @staticmethod
    def _inserted(order: np.ndarray, keys, where: list[int], added: list[int], new_keys: list) -> tuple:
        order = np.insert(order, where, added)
        if isinstance(keys, np.ndarray):
            return order, np.insert(keys, where, new_keys)
        objects = np.empty(len(keys), dtype=object)
        objects[:] = keys
        more = np.empty(len(new_keys), dtype=object)
        more[:] = new_keys
        return order, np.insert(objects, where, more).tolist()

    def _merged(self, order: np.ndarray, keys, added: list[int], new_keys: list) -> tuple:
        """``order`` and its ``keys`` with rows ``added`` (keys ``new_keys``) each where the sort puts it."""
        descending = self._sort[1] and not isinstance(keys, np.ndarray)
        by_key = sorted(range(len(added)), key=new_keys.__getitem__, reverse=descending)
        added, new_keys = [added[i] for i in by_key], [new_keys[i] for i in by_key]
        return self._inserted(order, keys, [self._place(k, keys) for k in new_keys], added, new_keys)

    def _place(self, k, keys=None) -> int:
        """Where a row with key ``k`` goes among the rows shown (or ``keys``): after any equal keys, as a stable sort
        puts it."""
        keys = self._keys if keys is None else keys
        if isinstance(keys, np.ndarray):
            return int(np.searchsorted(keys, k, side="right"))
        descending = self._sort[1]
        lo, hi = 0, len(keys)
        while lo < hi:
            mid = (lo + hi) // 2
            if (k > keys[mid]) if descending else (k < keys[mid]):
                hi = mid
            else:
                lo = mid + 1
        return lo

    def remove(self, positions) -> None:
        """Stop showing the rows at these positions."""
        gone = np.array([p for p in (int(p) for p in positions) if self._is_shown(p)], np.int64)
        if not len(gone):
            return
        keep = ~np.isin(self.order, gone)
        self._edits += 1
        self._late = [p for p in self._late if p not in set(gone.tolist())]
        # Moved to the end, then taken off there: a layout change can't take rows away.
        moved, keys = np.concatenate([self.order[keep], self.order[~keep]]), self._keys
        if keys is not None and len(keys) == len(self.order):
            keys = (np.concatenate([keys[keep], keys[~keep]]) if isinstance(keys, np.ndarray)
                    else [k for k, on in zip(keys, keep.tolist()) if on] + [k for k, on in zip(keys, keep.tolist()) if not on])
        else:
            keys = None
        self._relayout(moved, keys)
        kept = int(keep.sum())
        self.beginRemoveRows(QModelIndex(), kept, len(moved) - 1)
        self.order = moved[:kept]
        self._keys = None if keys is None else keys[:kept]
        self._shown[gone] = False
        self.endRemoveRows()

    def refresh(self, positions) -> None:
        """Rows at these positions changed (saved again, scored again): they're drawn as they are now, and the rows
        are put back in order when a sort key changed."""
        changed = [p for p in (int(p) for p in positions) if self._is_shown(p)]
        if not changed:
            return
        self._edits += 1
        moved = False
        if self._sort is not None and not self._sorting and self._keys is not None:
            rows = np.flatnonzero(np.isin(self.order, changed))
            for i in rows.tolist():
                k = self._key_at(self.source, int(self.order[i]))
                if k != self._keys[i]:
                    self._keys[i], moved = k, True
        self.dataChanged.emit(self.index(0, 0), self.index(len(self.order) - 1, len(self.columns) - 1))
        if moved:  # an updated score can move a row; a new label alone, sorted by date, can't
            if len(self.order) < self.BIG:
                self._resort(kept=True)
            else:
                self._slices.start(self._resort_steps(kept=True), lambda: None)

    def _tick(self) -> None:
        now = self._clock()
        for p in [p for p, t in self._born.items() if now - t >= FADE_SECONDS]:
            del self._born[p]
        if len(self.order):  # the view repaints only what is on screen
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.order) - 1, len(self.columns) - 1),
                                  [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole])
        if not self._born:
            self._fade.stop()

    def _fade_in(self, row: int) -> float | None:
        """0 to 1 while ``row`` is fading in, else None."""
        if not self._born:
            return None
        born = self._born.get(int(self.order[row]))
        if born is None:
            return None
        t = min(1.0, (self._clock() - born) / FADE_SECONDS)
        return 1 - (1 - t) ** 3  # ease out

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.order)

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
        _, key, fmt = self.columns[index.column()]
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
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter) if key in NUMERIC else None
        if role not in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole, SORT_ROLE):
            return None
        row = self.row_at(index.row())
        value = row[key]
        if role == Qt.ItemDataRole.DisplayRole:
            if value is None:
                return "–" if key in PENDING else ""
            return fmt(value)
        if role == SORT_ROLE:
            return self._key_of(index.column())(row)
        if key in PENDING and value is None:
            return PENDING_TIP
        if key in ("title", "body"):
            return (row["title"] + "\n\n" + (row["selftext"] or "")) if key == "title" else row["body"]
        return None

    def row_at(self, i: int):
        """The row shown ``i``-th, read with the rows around it when it isn't kept yet (it's being drawn)."""
        p = int(self.order[i])
        return self.source.row(p, around=self.order[max(0, i - RowSource.READ // 4):i + RowSource.READ].tolist())


class TablePage(QWidget):
    """Filterable, sortable table in an outlined box; double-click opens the item on Reddit.

    It shows some rows of a source (rows.RowSource): `set_rows` gives it the source and their positions, `load` a list.
    A big table (RowsModel.BIG rows on) is filtered and sorted away from the frames, its rows shown once they're
    all worked out; until then it shows what it did, and rows a fetch saves meanwhile wait for it."""

    SHRINKS = ("author", "post_title")  # free text that can end in "…" to leave room for the main text
    OPTIONAL = ("author", "post_title", "num_comments")  # hidden in this order when there's still no room
    FIXED = ("sentiment", "created_utc")  # written in a fixed format: "+0.123", "2026-10-20 03:54"

    def __init__(self, columns, stretch_col: int):
        super().__init__()
        self.model = RowsModel(columns)
        self.source: RowSource = RowList()
        self._base = np.zeros(0, np.int64)  # the positions of the rows given, before the label and text filters
        self._in_base = np.zeros(0, bool)  # by position: in _base
        self._text_key = columns[stretch_col][1]
        self._builds = 0  # counts loads and filterings, so one worked out between frames sees it's been overtaken
        self._waiting: list | None = None  # while one is: [(rows added, or None), (rows saved again, or None)], in turn
        self._last: tuple | None = None  # (label, needle, the positions they let through), for the next keystroke
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
        for sig in (self.model.rowsInserted, self.model.rowsRemoved, self.model.modelReset, self.model.layoutChanged):
            sig.connect(self._update_count)

    @property
    def all_rows(self) -> Rows:
        """Every row given or added, whatever the filters show."""
        return Rows(self.source, self._base)

    def load(self, rows) -> None:
        """Show a list of rows (the table keeps them: rows a fetch adds are added to it)."""
        self.set_rows(RowList(rows))

    def load_steps(self, rows):
        """load, a step at a time between frames for a big table: for Slices."""
        yield from self.set_rows_steps(RowList(rows))

    def set_rows(self, source: RowSource, at=None) -> None:
        """Show the rows of ``source`` at positions ``at`` (every row for None)."""
        finish(self._loading(source, at, None))

    def set_rows_steps(self, source: RowSource, at=None):
        """set_rows, a step at a time between frames for a big table: for Slices (the window's span)."""
        yield from self._loading(source, at, self.model.STEP)

    def _loading(self, source: RowSource, at, step: int | None):
        self._slices.stop()
        self._builds += 1
        builds = self._builds
        self.source, self._last = source, None
        self._set_base(np.arange(len(source)) if at is None else np.asarray(at, np.int64))
        self._waiting = []
        yield from self._filtering(step, builds)

    def _set_base(self, at: np.ndarray) -> None:
        self._base = at
        self._in_base = np.zeros(len(self.source), bool)
        self._in_base[at] = True

    def _in(self, p: int) -> bool:
        return p < len(self._in_base) and bool(self._in_base[p])

    def _add_base(self, positions: list[int]) -> None:
        if len(self._in_base) < len(self.source):
            self._in_base = np.concatenate([self._in_base, np.zeros(len(self.source) - len(self._in_base), bool)])
        self._base = np.concatenate([self._base, np.array(positions, np.int64)])
        self._in_base[positions] = True

    def _positions(self, rows) -> list[int]:
        """The positions of ``rows`` (rows a fetch saved) in the source, which takes in those it doesn't have yet
        first (a list given to `load` takes them all, in place of what it had)."""
        if isinstance(self.source, RowList):
            self.source.add(rows)
        else:  # (the window's sources have them already)
            at = self.source.where([r["id"] for r in rows])
            self.source.add([r for r, p in zip(rows, at) if p < 0])
        return [int(p) for p in self.source.where([r["id"] for r in rows]) if p >= 0]

    def add_rows(self, rows) -> int:
        """Show rows a running fetch has just saved; they fade in. Returns how many were new (or are to be
        looked at, while a big table is being worked out)."""
        rows = list(rows)
        if self._waiting is not None:
            self._waiting.append((rows, None))
            return len(rows)
        new = [p for p in dict.fromkeys(self._positions(rows)) if not self._in(p)]
        if new:
            self._add_base(new)
            self._last = None
            self.model.insert(self._picked(np.array(new, np.int64), *self._wanted()))
        return len(new)

    def update_rows(self, rows) -> int:
        """Bring in rows a finished fetch saved: ones saved again (new scores) replace the rows shown, the rest
        are added and fade in. Returns how many of the rows given were shown before."""
        rows = list(rows)
        if self._waiting is not None:
            self._waiting.append((None, rows))
            return 0
        return self.took(self._positions(rows))

    def took(self, at) -> int:
        """The rows at positions ``at`` were saved (again, or scored again): rows shown change to how they are now,
        and the rest are added and fade in. Returns how many were in the table before."""
        if self._waiting is not None:
            self._waiting.append((None, Rows(self.source, at)))
            return 0
        at = list(dict.fromkeys(int(p) for p in at))
        old = [p for p in at if self._in(p)]
        new = [p for p in at if not self._in(p)]
        if new:
            self._add_base(new)
        self._last = None
        # A row saved again can change its label (and so leave the label filter) or text.
        passing = set(self._picked(np.array(at, np.int64), *self._wanted()).tolist())
        self.model.remove([p for p in old if p not in passing])
        self.model.refresh([p for p in old if p in passing])
        self.model.insert([p for p in at if p in passing])
        self._update_count()
        return len(old)

    def retain(self, at) -> None:
        """Show the rows at ``at`` now, keeping the view's place: rows that left go, new ones are added (a fetch
        rescored some below the score filter, say)."""
        at = np.asarray(at, np.int64)
        keep = np.zeros(len(self.source), bool)
        keep[at] = True
        gone = self._base[~keep[self._base]]
        new = at[~np.isin(at, self._base)] if len(self._base) else at
        if not len(gone) and not len(new):
            return
        self._set_base(self._base[keep[self._base]])
        self.model.remove(gone)
        self.took(new)

    def _wanted(self) -> tuple[str, str]:
        """The label filter lowercased, and the text filter lowercased on one line."""
        return self.label_filter.currentText().lower(), " ".join(self.filter.text().lower().split())

    def _picked(self, at: np.ndarray, label: str, needle: str) -> np.ndarray:
        """The positions among ``at`` the label and text filters let through: the text on one line, ignoring case."""
        if label != "all labels" and len(at):
            at = at[self.source.label[at] == LABEL_AT[label]]
        if needle and len(at):
            texts = self.source.values(self._text_key)
            if texts is None:  # a few rows a fetch added: read here
                texts = dict(zip(at.tolist(), column(self.source.rows(at), self._text_key)))
            if " " in needle:  # line breaks and runs of spaces count as one space
                at = np.array([p for p in at.tolist() if (t := texts[p]) and needle in " ".join(t.lower().split())], np.int64)
            else:  # which they do anyway for a single word, so it's looked for as it is
                at = np.array([p for p in at.tolist() if (t := texts[p]) and needle in t.lower()], np.int64)
        return at

    def _apply_filters(self) -> None:
        if len(self._base) < self.model.BIG:
            finish(self._filtering(None))
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
        at, last, source = self._base, self._last, self.source
        if label == "all labels" and not needle:
            picked = at
        else:
            if needle and source.values(self._text_key) is None:  # every row's text, read once
                if step is None:
                    finish(source.read_values(self._text_key))
                for item in source.read_values(self._text_key):  # (nothing left to read when it was just read)
                    yield item
                    if builds != self._builds:
                        return
            if last is not None and last[0] == label and last[1] and last[1] in needle:
                at = last[2]  # a longer needle: only rows with the shorter one can have it
            parts = []
            for a in range(0, len(at), step or len(at) or 1):
                parts.append(self._picked(at[a:a + (step or len(at))], label, needle))
                if step is not None:
                    yield
                    if builds != self._builds:
                        return
            picked = np.concatenate(parts) if parts else at[:0]
        chosen = self.view.selectionModel().selectedRows()
        selected = {int(self.model.order[i.row()]) for i in chosen}
        current = self.view.currentIndex()
        current_at = int(self.model.order[current.row()]) if current.isValid() else None
        if step is None or len(picked) < self.model.BIG:
            self.model.show(source, picked)
        else:
            order = yield from self.model.show_steps(source, picked)
            if order is None or builds != self._builds:
                return
        self._last = (label, needle, picked) if needle else None
        if current_at is not None or 0 < len(selected) <= 1000:  # rows picked before still are, if the filters show them
            wanted = np.array(sorted(selected | ({current_at} - {None})), np.int64)
            for i in np.flatnonzero(np.isin(self.model.order, wanted)).tolist():
                p = int(self.model.order[i])
                if p == current_at:
                    self.view.selectionModel().setCurrentIndex(
                        self.model.index(i, current.column()), QItemSelectionModel.SelectionFlag.NoUpdate)
                if p in selected:
                    self.view.selectionModel().select(
                        self.model.index(i, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        if not self.filter.text():  # typing a filter keeps the columns as they are
            self._fit_due = True
            if self.isVisible():  # a page out of sight sizes them when it next shows
                self._fit_columns()
        self._update_count()
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
        order = self.model.order
        if not len(order):
            return 0
        _, key, fmt = self.model.columns[col]
        if key in self.FIXED:  # every value is written the same length: sample both ends of the sort order,
            values = self._values_at(key, np.concatenate([order[:30], order[-30:]]))  # so both signs and many digits are measured
        else:
            # Every row up to 10,000, then an even spread of them: going through 50,000 sorted rows took 20 ms a
            # column, and a widest value that common turns up in the spread too.
            values = self._values_at(key, order if len(order) <= 10000 else order[::len(order) // 10000 + 1])
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

    def _values_at(self, key: str, at: np.ndarray) -> set:
        """The distinct values of ``key`` among the rows at ``at``, as rows hold them. Text not read for every row
        is read for the first rows shown (and a few last) of a source read from disk: the ones drawn first."""
        source = self.model.source
        got = source.values(key)
        if isinstance(got, np.ndarray):
            got = got[at]
            if key == "label":
                return {LABELS[k] if k >= 0 else None for k in got.tolist()}
            if key == "score":
                return {None if v == NO_SCORE else v for v in got.tolist()}
            if key == "created_utc":
                return {None if v == -np.inf else v for v in got.tolist()}
            return set(got.tolist())
        if got is not None:
            return {got[p] for p in at.tolist()}
        if not isinstance(source, RowList):
            order = self.model.order
            at = np.concatenate([order[:60], order[-20:]])
        return set(column(source.rows(at), key))

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
