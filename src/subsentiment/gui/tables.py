"""The Posts and Comments tables: their model and the filterable page that shows one."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QFontMetrics, QIcon, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFrame, QHBoxLayout, QHeaderView, QLineEdit, QTableView, QVBoxLayout, QWidget,
)

from subsentiment.gui.charts import palette
from subsentiment.gui.widgets import label, panel, plural

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
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
    def __init__(self, columns: list[Column], parent=None, clock: Callable[[], float] = time.monotonic):
        super().__init__(parent)
        self.columns = columns
        self.rows: list = []
        self._icons: dict[str, QIcon] = {}
        self._colors: dict = {}
        self._clock = clock
        self._born: dict[int, float] = {}  # row index -> when it arrived, while it is fading in
        self._fade = QTimer(self)
        self._fade.setInterval(33)
        self._fade.timeout.connect(self._tick)

    def set_rows(self, rows: list, animate: bool = False) -> None:
        """Show ``rows``. With ``animate``, when they are the current rows plus new ones (a fetch in
        progress), the new ones are appended and fade in, and the view keeps its place."""
        old = {r["id"]: i for i, r in enumerate(self.rows)}
        if animate and self.rows and old.keys() <= {r["id"] for r in rows}:
            added = [r for r in rows if r["id"] not in old]
            for r in rows:  # scores and sentiment may have been updated
                if r["id"] in old:
                    self.rows[old[r["id"]]] = r
            if self.rows:
                self.dataChanged.emit(self.index(0, 0), self.index(len(self.rows) - 1, len(self.columns) - 1))
            self._insert(added)
            return
        self.beginResetModel()
        self.rows = list(rows)
        self._born.clear()
        self._fade.stop()
        self._load_colors()
        self.endResetModel()

    def append_rows(self, rows: list) -> int:
        """Add rows not shown yet, each fading in from now; returns how many were new."""
        have = {r["id"] for r in self.rows}
        added = [r for r in rows if r["id"] not in have]
        if not self._colors:
            self._load_colors()
        self._insert(added)
        return len(added)

    def _insert(self, added: list) -> None:
        """Append ``added`` at the end, each fading in from now."""
        if not added:
            return
        start = len(self.rows)
        self.beginInsertRows(QModelIndex(), start, start + len(added) - 1)
        self.rows.extend(added)
        self.endInsertRows()
        now = self._clock()
        self._born.update({i: now for i in range(start, start + len(added))})
        self._fade.start()

    def _load_colors(self) -> None:
        self._colors = palette()
        self._icons = {k: _swatch(self._colors[k]) for k in ("positive", "neutral", "negative")}

    def _tick(self) -> None:
        now = self._clock()
        done = [i for i, t in self._born.items() if now - t >= FADE_SECONDS]
        rows = list(self._born)
        for i in done:
            del self._born[i]
        if rows:
            self.dataChanged.emit(self.index(min(rows), 0), self.index(max(rows), len(self.columns) - 1),
                                  [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole])
        if not self._born:
            self._fade.stop()

    def _fade_in(self, row: int) -> float | None:
        """0 to 1 while ``row`` is fading in, else None."""
        born = self._born.get(row)
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
            if value is None:
                # Below every real number, so pending rows don't land among (and tie with) scores of -1.
                return float("-inf") if key in NUMERIC else ""
            return value
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
        font = QFont(self.view.font())  # the font the stylesheet draws headers with (8.5pt semibold)
        font.setPointSizeF(8.5)
        font.setWeight(QFont.Weight.DemiBold)
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
        self.count.setText(plural(self.proxy.rowCount(), "row"))

    def _open(self, index) -> None:
        row = self.model.row_at(self.proxy.mapToSource(index).row())
        QDesktopServices.openUrl(QUrl(row["permalink"]))
