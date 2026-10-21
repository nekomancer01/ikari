"""The Posts and Comments tables: their model and the filterable page that shows one."""
from __future__ import annotations

import heapq
import sqlite3
import time
from operator import itemgetter
from datetime import datetime
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


def column(rows: list, key: str):
    """Every row's ``key``, quickly: a saved row (sqlite3.Row) finds a column by name by comparing it with each
    name in turn, so it's read by position instead."""
    if rows and set(map(type, rows)) == {sqlite3.Row}:  # rows a fetch adds can be dicts
        return map(itemgetter(rows[0].keys().index(key)), rows)
    return map(itemgetter(key), rows)


class RowsModel(QAbstractTableModel):
    """The rows a table shows, sorted here in Python.

    Qt's QSortFilterProxyModel sorted by asking this model for each value through data(), a Python call
    per comparison: about 3.4 million of them, and 17 seconds, for a subreddit with 100,000 comments."""

    def __init__(self, columns: list[Column], parent=None, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.columns = columns
        self.rows: list = []
        self._ids: set[str] = set()  # the ids in self.rows
        self._colors: dict = {}
        self._clock = clock
        self._sort: tuple[int, bool] | None = None  # (column, descending) once the view has sorted
        self._keys: list = []  # each row's sort key, in step with self.rows while sorted
        self._born: dict[str, float] = {}  # row id -> when it arrived, while it is fading in
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._tick)

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
            key = self.columns[self._sort[0]][1]
            missing = float("-inf") if key in NUMERIC or key == "created_utc" else ""
            keys = [missing if v is None else v for v in column(rows, key)]  # as _key_of, without a call a row
        order = sorted(range(len(rows)), key=keys.__getitem__, reverse=self._sort[1])
        self._keys = [keys[i] for i in order]
        return [rows[i] for i in order]

    def sort(self, column: int, order=Qt.SortOrder.AscendingOrder) -> None:
        """Called by the view when a header is clicked. Selected rows stay selected."""
        wanted = (column, order == Qt.SortOrder.DescendingOrder)
        if wanted != self._sort:  # the view asks twice for one sortByColumn
            self._sort = wanted
            self._resort()

    def _resort(self, keys_kept: bool = False) -> None:
        """Put the rows back in sort order; with ``keys_kept``, self._keys still holds each row's key."""
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        ids = [self.rows[i.row()]["id"] for i in old]
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
        self.beginResetModel()
        self.rows = self._sorted(rows)
        self._ids = set(map(itemgetter("id"), self.rows))
        self._born.clear()
        self._fade.stop()
        self._restyle()
        self.endResetModel()

    def update_rows(self, rows: list) -> None:
        """Rows saved again (with new scores) replace the ones shown; rows not shown yet are added and fade
        in. The view keeps its place."""
        where = {r["id"]: i for i, r in enumerate(self.rows)} if rows else {}
        key_of = self._key_of(self._sort[0]) if self._sort is not None else None
        added, changed, moved = [], False, False
        for r in rows:
            i = where.get(r["id"])
            if i is None:
                added.append(r)
            elif self.rows[i] != r:
                self.rows[i] = r
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
            if resort and self._sort is not None:
                self._resort(keys_kept=True)
            return
        self._ids.update(r["id"] for r in added)
        now = self._clock()
        if self._sort is None or resort or len(added) > 1000:  # one insert at the end, then one re-sort
            start = len(self.rows)
            self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
            self.rows.extend(added)
            if self._sort is not None:
                self._keys.extend(map(self._key_of(self._sort[0]), added))
            self.endInsertRows()
            if self._sort is not None:
                self._resort(keys_kept=True)
        else:
            descending, key_of = self._sort[1], self._key_of(self._sort[0])
            for r in added:
                k = key_of(r)
                lo, hi = 0, len(self._keys)
                while lo < hi:  # after any equal keys, as a stable sort would put it
                    mid = (lo + hi) // 2
                    if (k > self._keys[mid]) if descending else (k < self._keys[mid]):
                        hi = mid
                    else:
                        lo = mid + 1
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
    """Filterable, sortable table in an outlined box; double-click opens the item on Reddit."""

    SHRINKS = ("author", "post_title")  # free text that can end in "…" to leave room for the main text
    OPTIONAL = ("author", "post_title", "num_comments")  # hidden in this order when there's still no room
    FIXED = ("sentiment", "created_utc")  # written in a fixed format: "+0.123", "2026-10-20 03:54"

    def __init__(self, columns, stretch_col: int):
        super().__init__()
        self.model = RowsModel(columns)
        self._all_rows: list = []
        self._ids: set[str] = set()
        self._text_key = columns[stretch_col][1]
        self._haystacks: dict[str, str] = {}  # row id -> its text lowercased on one line, once filtered by

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

    def load(self, rows) -> None:
        self._all_rows = rows
        self._ids = set(map(itemgetter("id"), rows))
        self._haystacks = {}
        self._apply_filters()

    def add_rows(self, rows) -> int:
        """Add rows a running fetch has just saved; they fade in. Returns how many were new."""
        new = [r for r in rows if r["id"] not in self._ids]
        self._ids.update(r["id"] for r in new)
        self._all_rows.extend(new)
        shows = self._matcher()
        self.model.append_rows([r for r in new if shows is None or shows(r)])
        return len(new)

    def update_rows(self, rows) -> int:
        """Bring in rows a finished fetch saved: ones saved again (new scores) replace the rows shown, the
        rest are added and fade in. Returns how many shown rows changed."""
        where = {r["id"]: i for i, r in enumerate(self._all_rows)}
        new, changed = [], []
        for r in rows:
            i = where.get(r["id"])
            if i is None:
                new.append(r)
            elif self._all_rows[i] != r:  # most were dripped in as they were saved, and are unchanged
                self._all_rows[i] = r
                self._haystacks.pop(r["id"], None)
                changed.append(r)
        self._ids.update(r["id"] for r in new)
        self._all_rows.extend(new)
        shows = self._matcher()
        if shows is None:
            self.model.update_rows(changed + new)
        else:  # a rescored row can change its label
            self.model.set_rows(list(filter(shows, self._all_rows)), animate=True)
        self._update_count()
        return len(changed)

    def _matcher(self):
        """Whether a row passes the label and text filters, or None when nothing is filtered."""
        label = self.label_filter.currentText().lower()
        needle = " ".join(self.filter.text().lower().split())
        if label == "all labels" and not needle:
            return None
        key, hays = self._text_key, self._haystacks

        def shows(row) -> bool:
            if label != "all labels" and row["label"] != label:
                return False
            if not needle:
                return True
            hay = hays.get(row["id"])
            if hay is None:  # the text on one line, lowercased, kept for the next keystroke
                hay = hays[row["id"]] = " ".join((row[key] or "").lower().split())
            return needle in hay
        return shows

    def _apply_filters(self) -> None:
        shows = self._matcher()
        if not self.filter.text():
            self._haystacks = {}  # a copy of every row's text; only needed while there is a filter
        chosen = self.view.selectionModel().selectedRows()
        selected = {self.model.row_at(i.row())["id"] for i in chosen}
        current = self.view.currentIndex()
        current_id = self.model.row_at(current.row())["id"] if current.isValid() else None
        self.model.set_rows(self._all_rows if shows is None else list(filter(shows, self._all_rows)))
        if current_id or 0 < len(selected) <= 1000:  # rows picked before still are, if the filters show them
            for i, r in enumerate(self.model.rows):
                if r["id"] == current_id:
                    self.view.selectionModel().setCurrentIndex(
                        self.model.index(i, current.column()), QItemSelectionModel.SelectionFlag.NoUpdate)
                if r["id"] in selected:
                    self.view.selectionModel().select(
                        self.model.index(i, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
        if not self.filter.text():  # typing a filter keeps the columns as they are
            self._fit_due = True
            if self.isVisible():  # a page out of sight sizes them when it next shows
                self._fit_columns()
        self._update_count()

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
