"""Table models for posts and comments."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPixmap

from subsentiment.gui.charts import palette

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


def _swatch(color: str) -> QIcon:
    pm = QPixmap(10, 10)
    pm.fill(QColor(color))
    return QIcon(pm)


class RowsModel(QAbstractTableModel):
    """The rows a table shows, sorted here in Python.

    Qt's QSortFilterProxyModel sorted by asking this model for each value through data(), a Python call
    per comparison: about 3.4 million of them, and 17 seconds, for a subreddit with 100,000 comments."""

    def __init__(self, columns: list[Column], parent=None, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.columns = columns
        self.rows: list = []
        self._ids: set[str] = set()  # the ids in self.rows
        self._icons: dict[str, QIcon] = {}
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
        missing = -1 if key in NUMERIC or key == "created_utc" else ""  # sorts alongside the real values
        return lambda row: missing if (v := row[key]) is None else v

    def _sorted(self, rows: list, keys: list | None = None) -> list:
        """``rows`` in the current sort order; also sets self._keys to match. ``keys`` are the rows' sort
        keys when the caller has them: rows that are already mostly in order then sort in about one pass."""
        if self._sort is None:
            self._keys = []
            return list(rows)
        if keys is None:
            keys = list(map(self._key_of(self._sort[0]), rows))
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
        self._icons = {k: _swatch(self._colors[k]) for k in ("positive", "neutral", "negative")}

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
        self._ids = {r["id"] for r in self.rows}
        self._born.clear()
        self._fade.stop()
        self._restyle()
        self.endResetModel()

    def update_rows(self, rows: list) -> None:
        """Rows saved again (with new scores) replace the ones shown; rows not shown yet are added and fade
        in. The view keeps its place."""
        where = {r["id"]: i for i, r in enumerate(self.rows)} if rows else {}
        key_of = self._key_of(self._sort[0]) if self._sort is not None else None
        added, changed = [], False
        for r in rows:
            i = where.get(r["id"])
            if i is None:
                added.append(r)
            elif self.rows[i] != r:
                self.rows[i] = r
                if key_of is not None:
                    self._keys[i] = key_of(r)
                changed = True
        if changed:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))
        self._insert(added, resort=changed)  # an updated score can move a row

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
        if role == Qt.ItemDataRole.DecorationRole and key == "label":
            return self._icons.get(value)
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
