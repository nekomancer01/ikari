"""Table models for posts and comments."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QIcon, QPixmap

from subsentiment.gui.charts import palette

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
NUMERIC = ("sentiment", "score", "num_comments")
PENDING = ("score", "num_comments")  # None until Arctic Shift re-fetches the item, about 36 hours in
PENDING_TIP = "Arctic Shift fills this in about 36 hours after posting. Fetch this range again later to update it."


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
    def __init__(self, columns: list[Column], parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows: list = []
        self._icons: dict[str, QIcon] = {}

    def set_rows(self, rows: list) -> None:
        self.beginResetModel()
        self.rows = rows
        colors = palette()
        self._icons = {k: _swatch(colors[k]) for k in ("positive", "neutral", "negative")}
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.columns[section][0]
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
            if value is None:
                return -1 if key in NUMERIC else ""  # keeps unknown numbers sortable alongside real ones
            return value
        if role == Qt.ItemDataRole.DecorationRole and key == "label":
            return self._icons.get(value)
        if role == Qt.ItemDataRole.ToolTipRole and key in PENDING and value is None:
            return PENDING_TIP
        if role == Qt.ItemDataRole.ToolTipRole and key in ("title", "body"):
            return (row["title"] + "\n\n" + (row["selftext"] or "")) if key == "title" else row["body"]
        if role == Qt.ItemDataRole.TextAlignmentRole and key in NUMERIC:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None

    def row_at(self, i: int):
        return self.rows[i]
