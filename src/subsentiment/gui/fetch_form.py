"""The sidebar's Fetch fields: what to fetch, remembered between sessions, turned into a FetchRequest."""
from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import QDate, QObject, QSettings, Qt, Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLineEdit, QWidget

from subsentiment import collect
from subsentiment.gui.widgets import CommentCap, DateEdit, label

# Date ranges the fetch form offers: key -> (label, days back from now). "custom" uses the From/To dates.
RANGES = {"day": ("Past 24 hours", 1), "week": ("Past week", 7), "month": ("Past 30 days", 30),
          "quarter": ("Past 3 months", 91), "year": ("Past year", 365), "custom": ("Custom dates", None)}

# Last-used fetch form, restored at startup. Keys live under "fetch/" in QSettings.
# A fetch downloads every post in the range, so the old "fetch/pick" and "fetch/limit" keys are no longer read.
# The CommentCap saves "comments_per_post" (default here) and "comment_pick" under "fetch/" itself.
FETCH_DEFAULTS = {"subreddit": "", "query": "", "range": "week", "from": "", "to": "",
                  "include_comments": True, "comments_per_post": 50, "refresh_scores": False}


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


def date_edit() -> DateEdit:
    edit = DateEdit()
    edit.setDisplayFormat("d MMM yyyy")
    edit.setMinimumDate(QDate(2005, 6, 1))  # Reddit's first month
    edit.setMaximumDate(QDate.currentDate())
    edit.setMinimumWidth(110)  # its size hint is wide enough to squeeze the form's labels
    return edit


class FetchForm(QObject):
    """The fields and their ``layout``, which the sidebar shows. Enter in a text field sends ``submitted``;
    a change of range, a preset or a typed date, sends ``range_moved``."""

    submitted = Signal()
    range_moved = Signal()

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._busy = False
        self.subreddit = QLineEdit()
        self.subreddit.setPlaceholderText("e.g. python")
        self.subreddit.setToolTip("A subreddit name; r/name and reddit.com links work too.")
        self.subreddit.returnPressed.connect(self.submitted)
        self.query = QLineEdit()
        self.query.setPlaceholderText("Optional keywords")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.submitted)
        self.range = QComboBox()
        for key, (text, _) in RANGES.items():
            self.range.addItem(text, key)
        self.range.currentIndexChanged.connect(self._on_range_changed)
        self.date_from = date_edit()
        self.date_to = date_edit()
        for edit in (self.date_from, self.date_to):
            edit.userDateChanged.connect(self._on_date_edited)
        self.include_comments = QCheckBox("Fetch")
        # 0 is "All": every comment on each post, as Download coverage's Download all comments fetches.
        self.comment_cap = CommentCap(settings, "fetch", FETCH_DEFAULTS["comments_per_post"], 500, self)
        self.comments_per_post = self.comment_cap.count
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
        self.restore()

        self.layout = form = QFormLayout()
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(11)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.addRow(label("Subreddit", "muted"), self.subreddit)
        form.addRow(label("Search", "muted"), self.query)
        form.addRow(label("Posted", "muted"), self.range)
        form.addRow(label("From", "muted"), self.date_from)
        form.addRow(label("To", "muted"), self.date_to)
        form.addRow(label("Comments", "muted"), comments_row)
        form.addRow(label("Keep", "muted"), self.comment_cap.pick)
        form.addRow(label("Scores", "muted"), self.refresh_scores)

    def align_labels(self) -> None:
        """Each label as tall as its field and centred in it: QFormLayout top-aligns a short label, which put
        labels a few pixels above their fields' centres where the text is small (macOS). Called once the
        layout is in the sidebar, so the fields have their styled sizes."""
        form = self.layout
        for row in range(form.rowCount()):
            name = form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
            field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
            name.setMinimumHeight(field.sizeHint().height())
            name.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

    def bounds(self) -> tuple[float, float]:
        """The date range as (after, before), before being now for "up to now"."""
        after, before = range_bounds(self.range.currentData(), self.date_from.date(), self.date_to.date())
        return after, before if before is not None else datetime.now().timestamp()

    def request(self, name: str) -> collect.FetchRequest:
        """A fetch of every post in the range for ``name``, remembering the form for next time."""
        self.subreddit.setText(name)
        self.save()
        self._on_range_changed()  # a preset counts back from now, not from when the app started
        after, before = range_bounds(self.range.currentData(), self.date_from.date(), self.date_to.date())
        return collect.FetchRequest(
            subreddit=name, after=after, before=before, limit=None, query=self.query.text().strip(),
            include_comments=self.include_comments.isChecked(), refresh_scores=self.refresh_scores.isChecked(),
            **self.comment_cap.request())

    def set_busy(self, busy: bool) -> None:
        """No changes while a fetch runs."""
        self._busy = busy
        for w in (self.subreddit, self.query, self.range, self.date_from, self.date_to,
                  self.include_comments, self.refresh_scores):
            w.setEnabled(not busy)
        self._update_comments_enabled()

    def restore(self) -> None:
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
        self.include_comments.setChecked(v["include_comments"])
        self.refresh_scores.setChecked(v["refresh_scores"])
        self.comment_cap.restore()
        self._update_comments_enabled()

    def save(self) -> None:
        for k, v in {"subreddit": self.subreddit.text().strip(), "query": self.query.text().strip(),
                     "range": self.range.currentData(),
                     "from": self.date_from.date().toString(Qt.DateFormat.ISODate),
                     "to": self.date_to.date().toString(Qt.DateFormat.ISODate),
                     "include_comments": self.include_comments.isChecked(),
                     "refresh_scores": self.refresh_scores.isChecked()}.items():
            self.settings.setValue(f"fetch/{k}", v)
        self.comment_cap.save()

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
        self.range_moved.emit()

    def _on_date_edited(self) -> None:
        """Typing or picking a date means a custom range."""
        self.range.blockSignals(True)
        self.range.setCurrentIndex(self.range.findData("custom"))
        self.range.blockSignals(False)
        self.range_moved.emit()

    def _update_comments_enabled(self) -> None:
        self.comment_cap.set_enabled(not self._busy and self.include_comments.isChecked())
