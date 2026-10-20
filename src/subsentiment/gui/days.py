"""Choosing which saved days the analysis covers.

The header's Days picker opens a month calendar of the subreddit's saved days. Any set of days can be
chosen, not only a run of them: click a day to add or drop it, drag across days, or shift-click to extend
from the last one clicked. The Overview, the Posts and Comments tables and CSV export then count only those
days. Nothing is fetched; days without saved data can't be picked. Days are UTC, like the timeline chart and
Download coverage.

The choice is remembered per subreddit. Every day (the default) is stored as no choice at all, so days a
later fetch adds are counted too; a narrower choice stays as picked.
"""
from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QToolTip, QVBoxLayout, QWidget,
)

from subsentiment.gui import theme

DAY = 86400
WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
SETTINGS_GROUP = "analysis_days"  # QSettings: analysis_days/<subreddit, lowercased> = "2026-10-17,2026-10-18"


def day_of(ts: float) -> int:
    """Start of the UTC day ``ts`` falls in, in epoch seconds."""
    return int(ts // DAY * DAY)


def as_date(day: int) -> date:
    return datetime.fromtimestamp(day, timezone.utc).date()


def from_date(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def keep(rows, chosen: set[int] | None) -> list:
    """The rows created on a chosen day; all of them when every day is chosen (None). Always a new list: the
    tables extend the list they were given with a running fetch's rows."""
    if chosen is None:
        return list(rows)
    return [r for r in rows if r["created_utc"] is not None and day_of(r["created_utc"]) in chosen]


def settings_key(subreddit: str) -> str:
    return f"{SETTINGS_GROUP}/{subreddit.lower()}"


def load_choice(settings, subreddit: str) -> set[int] | None:
    """The days chosen for a subreddit, or None for every day."""
    if not subreddit or not settings.contains(settings_key(subreddit)):
        return None
    text = settings.value(settings_key(subreddit), "", str)
    days = set()
    for part in text.split(","):
        try:
            days.add(from_date(date.fromisoformat(part.strip())))
        except ValueError:
            continue
    return days


def save_choice(settings, subreddit: str, chosen: set[int] | None, available) -> set[int] | None:
    """Remember a choice; one covering every saved day is stored as every day. Returns what was stored."""
    if chosen is not None and set(available) <= chosen:
        chosen = None
    if chosen is None:
        settings.remove(settings_key(subreddit))
    else:
        settings.setValue(settings_key(subreddit), ",".join(as_date(d).isoformat() for d in sorted(chosen)))
    return chosen


def short_date(day: int, year: bool = False) -> str:
    d = as_date(day)
    return f"{d.day} {d:%b %Y}" if year else f"{d.day} {d:%b}"


def choice_text(chosen: set[int] | None, available) -> str:
    """What the picker shows when closed: "All days", "17 Oct", "12 – 18 Oct", "5 of 12 days" or "No days"."""
    available = set(available)
    if chosen is None or (available and available <= chosen):
        return "All days"
    picked = sorted(chosen & available) if available else sorted(chosen)
    if not picked:
        return "No days"
    if len(picked) == 1:
        return short_date(picked[0])
    run = picked[-1] - picked[0] == (len(picked) - 1) * DAY  # a whole run of calendar days
    if run:
        a, b = as_date(picked[0]), as_date(picked[-1])
        if a.year != b.year:
            return f"{a.day} {a:%b %Y} – {b.day} {b:%b %Y}"
        return f"{a.day} – {b.day} {b:%b}" if a.month == b.month else f"{a.day} {a:%b} – {b.day} {b:%b}"
    return f"{len(picked)} of {len(available)} days"


def first_weekday() -> int:
    """0 for Monday … 6 for Sunday, from the system locale."""
    from PySide6.QtCore import QLocale
    return (QLocale.system().firstDayOfWeek().value - 1) % 7


class DayCalendar(QWidget):
    """One month of days as a grid: saved days can be chosen, the rest are faint.

    Each saved day carries a short bar under its number showing how much is saved that day, against the
    busiest day, so quiet days stand out."""

    changed = Signal()
    CELL = QSize(40, 36)
    GAP = 4
    HEAD = 22  # weekday initials

    def __init__(self):
        super().__init__()
        self.counts: dict[int, tuple[int, int]] = {}  # day -> (posts, comments)
        self.chosen: set[int] = set()
        self.year, self.month = 2026, 1
        self._hover: int | None = None
        self._focus: int | None = None
        self._drag: bool | None = None  # while dragging: True adds days, False drops them
        self._anchor: int | None = None
        self._keyboard = False  # the focus ring shows once the arrow keys are used, not after clicks
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._fit()

    def rows(self) -> int:
        """Weeks the month spans (4 to 6); the grid is only as tall as it needs."""
        last = max(i for i, d in enumerate(self.days_shown()) if d is not None)
        return last // 7 + 1

    def _fit(self) -> None:
        rows = self.rows()
        self.setFixedSize(7 * self.CELL.width() + 6 * self.GAP,
                          self.HEAD + rows * self.CELL.height() + (rows - 1) * self.GAP)

    # -- data ---------------------------------------------------------------

    def set_days(self, counts: dict[int, tuple[int, int]], chosen: set[int]) -> None:
        self.counts = dict(counts)
        self.chosen = set(chosen) & set(counts)
        self.update()

    def show_month(self, year: int, month: int) -> None:
        self.year, self.month = year, month
        self._hover = None
        self._fit()
        self.update()

    def days_shown(self) -> list[int | None]:
        """The 42 grid slots, row by row: a day start, or None outside this month."""
        first = date(self.year, self.month, 1)
        lead = (first.weekday() - first_weekday()) % 7
        length = calendar.monthrange(self.year, self.month)[1]
        slots: list[int | None] = [None] * lead
        slots += [from_date(first + timedelta(days=i)) for i in range(length)]
        return slots + [None] * (42 - len(slots))

    def cell_rect(self, index: int) -> QRect:
        row, col = divmod(index, 7)
        return QRect(col * (self.CELL.width() + self.GAP), self.HEAD + row * (self.CELL.height() + self.GAP),
                     self.CELL.width(), self.CELL.height())

    def day_at(self, pos: QPoint) -> int | None:
        for i, day in enumerate(self.days_shown()):
            if day is not None and self.cell_rect(i).contains(pos):
                return day
        return None

    def rect_of(self, day: int) -> QRect | None:
        slots = self.days_shown()
        return self.cell_rect(slots.index(day)) if day in slots else None

    # -- choosing -------------------------------------------------------------

    def _set(self, days, on: bool) -> None:
        days = {d for d in days if d in self.counts}
        before = set(self.chosen)
        self.chosen = (self.chosen | days) if on else (self.chosen - days)
        if self.chosen != before:
            self.update()
            self.changed.emit()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        day = self.day_at(event.position().toPoint())
        if day is None or day not in self.counts:
            return
        self._focus = day
        self._keyboard = False
        if event.modifiers() & Qt.KeyboardModifier.ShiftModifier and self._anchor is not None:
            lo, hi = sorted((self._anchor, day))
            self._set(range(lo, hi + DAY, DAY), self._anchor in self.chosen)
            return
        self._anchor = day
        self._drag = day not in self.chosen
        self._set([day], self._drag)

    def mouseMoveEvent(self, event) -> None:
        day = self.day_at(event.position().toPoint())
        hover = day if day in self.counts else None
        if hover != self._hover:
            self._hover = hover
            self.update()
            self.setCursor(Qt.CursorShape.PointingHandCursor if hover is not None else Qt.CursorShape.ArrowCursor)
        if self._drag is not None and hover is not None:
            self._set([hover], self._drag)
        if hover is not None:
            QToolTip.showText(event.globalPosition().toPoint(), self.tip(hover), self, self.rect_of(hover))
        else:
            QToolTip.hideText()

    def mouseReleaseEvent(self, event) -> None:
        self._drag = None

    def leaveEvent(self, event) -> None:
        self._hover = None
        self.update()

    def keyPressEvent(self, event) -> None:
        steps = {Qt.Key.Key_Left: -1, Qt.Key.Key_Right: 1, Qt.Key.Key_Up: -7, Qt.Key.Key_Down: 7}
        slots = [d for d in self.days_shown() if d is not None]
        if event.key() in steps and slots:
            at = self._focus if self._focus in slots else slots[0]
            target = at + steps[event.key()] * DAY
            self._focus = min(max(target, slots[0]), slots[-1])
            self._keyboard = True
            self.update()
            return
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter) and self._focus in self.counts:
            self._anchor = self._focus
            self._set([self._focus], self._focus not in self.chosen)
            return
        super().keyPressEvent(event)

    def tip(self, day: int) -> str:
        posts, comments = self.counts.get(day, (0, 0))
        d = as_date(day)
        parts = [f"{posts:,} post{'' if posts == 1 else 's'}"]
        if comments:
            parts.append(f"{comments:,} comment{'' if comments == 1 else 's'}")
        state = "counted" if day in self.chosen else "not counted"
        return f"{d:%a} {d.day} {d:%b %Y} (UTC) · {' and '.join(parts)} · {state}"

    # -- look -----------------------------------------------------------------

    def paintEvent(self, event) -> None:
        t = theme.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        small = QFont(self.font())
        small.setPointSizeF(8.5)
        small.setWeight(QFont.Weight.DemiBold)
        p.setFont(small)
        p.setPen(QColor(t["muted"]))
        start = first_weekday()
        for col in range(7):
            r = QRect(col * (self.CELL.width() + self.GAP), 0, self.CELL.width(), self.HEAD - 4)
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, WEEKDAYS[(start + col) % 7])

        busiest = max((a + b for a, b in self.counts.values()), default=0)
        number = QFont(self.font())
        number.setWeight(QFont.Weight.DemiBold)
        edge = QColor(*t["edge"][:3], min(255, t["edge"][3] * 2))
        soft = QColor(t["soft"])
        for i, day in enumerate(self.days_shown()):
            if day is None:
                continue
            r = QRectF(self.cell_rect(i)).adjusted(0.5, 0.5, -0.5, -0.5)
            saved = day in self.counts
            on = day in self.chosen
            path = QPainterPath()
            path.addRoundedRect(r, 9, 9)
            if on:
                p.fillPath(path, QColor(t["button"]))
                if day == self._hover:
                    p.fillPath(path, QColor(255, 255, 255, 30))
            elif saved:
                p.fillPath(path, soft)
                p.setPen(QPen(QColor(t["accent"]) if day == self._hover else edge, 1))
                p.drawPath(path)
            if day == self._focus and self._keyboard and self.hasFocus():
                p.setPen(QPen(QColor(t["accent"]), 1.5))
                p.drawRoundedRect(r.adjusted(-2, -2, 2, 2), 11, 11)
            p.setFont(number)
            p.setPen(QColor(t["on_accent"] if on else t["text"] if saved else t["faint"]))
            p.drawText(r.adjusted(0, 0, 0, -7 if saved else 0), Qt.AlignmentFlag.AlignCenter, str(as_date(day).day))
            if saved and busiest:
                n = sum(self.counts[day])
                # Square-root scale, so a day with a tenth of the busiest day's items still shows a visible bar.
                width = max(4.0, (r.width() - 16) * math.sqrt(n / busiest))
                bar = QRectF(r.center().x() - width / 2, r.bottom() - 8, width, 3)
                c = QColor(t["on_accent"]) if on else QColor(t["accent"])
                c.setAlpha(200 if on else 150)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(c)
                p.drawRoundedRect(bar, 1.5, 1.5)
                p.setBrush(Qt.BrushStyle.NoBrush)


def months_between(first: int, last: int) -> list[tuple[int, int]]:
    a, b = as_date(first), as_date(last)
    out = []
    y, m = a.year, a.month
    while (y, m) <= (b.year, b.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def flat_button(text: str) -> QPushButton:
    b = theme.mark(theme.mark(QPushButton(text), "flat", True), "small", True)
    b.setAutoDefault(False)
    b.setCursor(Qt.CursorShape.PointingHandCursor)
    return b


class DayPicker(QFrame):
    """The popup: a month of days with arrows to the other saved months, and quick choices below."""

    changed = Signal()
    closed = Signal()
    SHADOW = 12  # px around the card for its soft shadow

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.NoDropShadowWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # A click on the picker's own box closes the calendar; without this Qt replays it and reopens it.
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay)
        self.months: list[tuple[int, int]] = []
        lay = QVBoxLayout(self)
        s = self.SHADOW
        lay.setContentsMargins(s + 14, s + 12, s + 14, s + 14)
        lay.setSpacing(10)

        self.prev = flat_button("‹")
        self.next = flat_button("›")
        for b, step in ((self.prev, -1), (self.next, 1)):
            b.setFixedWidth(30)
            b.setToolTip("Previous month" if step < 0 else "Next month")
            b.clicked.connect(lambda _=False, k=step: self._step(k))
        self.title = QLabel()
        self.title.setFont(theme.display_font(11, 600))
        self.title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        head = QHBoxLayout()
        head.setSpacing(4)
        head.addWidget(self.prev)
        head.addWidget(self.title, 1)
        head.addWidget(self.next)
        lay.addLayout(head)

        self.calendar = DayCalendar()
        self.calendar.changed.connect(self._on_changed)
        lay.addWidget(self.calendar, 0, Qt.AlignmentFlag.AlignHCenter)

        self.summary = theme.mark(QLabel(), "role", "muted")
        lay.addWidget(self.summary)
        self.all_btn = flat_button("All days")
        self.all_btn.setToolTip("Count every saved day, including days fetched later")
        self.month_btn = flat_button("This month")
        self.month_btn.setToolTip("Count only the saved days of the month shown")
        self.week_btn = flat_button("Last 7 days")
        self.week_btn.setToolTip("Count the 7 most recent calendar days that have saved data")
        self.none_btn = flat_button("None")
        self.none_btn.setToolTip("Clear the choice, then click the days to count")
        self.all_btn.clicked.connect(lambda: self._choose(set(self.calendar.counts)))
        self.month_btn.clicked.connect(self._choose_month)
        self.week_btn.clicked.connect(self._choose_week)
        self.none_btn.clicked.connect(lambda: self._choose(set()))
        quick = QHBoxLayout()
        quick.setSpacing(2)
        for b in (self.all_btn, self.month_btn, self.week_btn, self.none_btn):
            quick.addWidget(b)
        quick.addStretch(1)
        lay.addLayout(quick)

    # -- data ---------------------------------------------------------------

    def set_days(self, counts: dict[int, tuple[int, int]], chosen: set[int] | None) -> None:
        """Saved days with their (posts, comments), and the chosen ones (None: all of them)."""
        self.calendar.set_days(counts, set(counts) if chosen is None else chosen)
        self.months = months_between(min(counts), max(counts)) if counts else []
        shown = (self.calendar.year, self.calendar.month)
        if shown not in self.months and self.months:
            latest = max(counts)
            self.calendar.show_month(as_date(latest).year, as_date(latest).month)
        self._refresh()

    def show_latest(self) -> None:
        """Open on the month of the latest chosen day, or of the latest saved day when none are chosen."""
        days = self.calendar.chosen or set(self.calendar.counts)
        if days:
            d = as_date(max(days))
            self.calendar.show_month(d.year, d.month)
            self._refresh()

    def chosen(self) -> set[int]:
        return set(self.calendar.chosen)

    def _step(self, k: int) -> None:
        shown = (self.calendar.year, self.calendar.month)
        if shown in self.months:
            i = min(max(self.months.index(shown) + k, 0), len(self.months) - 1)
            self.calendar.show_month(*self.months[i])
            self._refresh()
            self.adjustSize()  # a month spanning more or fewer weeks

    def _choose(self, days: set[int]) -> None:
        if days != self.calendar.chosen:
            self.calendar.chosen = set(days) & set(self.calendar.counts)
            self.calendar.update()
            self._on_changed()

    def _choose_month(self) -> None:
        y, m = self.calendar.year, self.calendar.month
        self._choose({d for d in self.calendar.counts if (as_date(d).year, as_date(d).month) == (y, m)})

    def _choose_week(self) -> None:
        if self.calendar.counts:
            last = max(self.calendar.counts)
            self._choose({d for d in self.calendar.counts if d > last - 7 * DAY})

    def _on_changed(self) -> None:
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        cal = self.calendar
        y, m = cal.year, cal.month
        self.title.setText(f"{calendar.month_name[m]} {y}")
        shown = (y, m)
        at = self.months.index(shown) if shown in self.months else -1
        self.prev.setEnabled(at > 0)
        self.next.setEnabled(0 <= at < len(self.months) - 1)
        days = cal.chosen
        items = sum(sum(cal.counts[d]) for d in days)
        n = len(cal.counts)
        if not n:
            self.summary.setText("Nothing saved yet")
        elif len(days) == n:
            self.summary.setText(f"All {n:,} saved day{'' if n == 1 else 's'} · {items:,} items")
        else:
            self.summary.setText(f"{len(days):,} of {n:,} days · {items:,} items")

    # -- look and placement ---------------------------------------------------

    def place_under(self, anchor: QWidget) -> None:
        """Below the anchor, right edges lined up, kept on its screen."""
        self.adjustSize()
        s = self.SHADOW
        bottom_right = anchor.mapToGlobal(QPoint(anchor.width(), anchor.height() + 6))
        x, y = bottom_right.x() - self.width() + s, bottom_right.y() - s
        screen = anchor.screen() or QGuiApplication.primaryScreen()
        if screen is not None:
            free = screen.availableGeometry()
            x = max(free.left() - s, min(x, free.right() - self.width() + s))
            if y + self.height() - s > free.bottom():  # no room below: open above instead
                y = anchor.mapToGlobal(QPoint(0, 0)).y() - 6 - self.height() + s
        self.move(x, y)

    def paintEvent(self, event) -> None:
        theme.paint_popup(self, self.SHADOW)

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self.closed.emit()


class DaysCombo(QComboBox):
    """Looks and opens like the header's other picker, but drops down the day calendar.

    ``chosen_changed`` sends the new choice, None for every day, a moment after the last click, so
    dragging across days doesn't redo the whole analysis for each day it passes."""

    chosen_changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.addItem("All days")
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.setToolTip("Choose which saved days the analysis counts")
        self.counts: dict[int, tuple[int, int]] = {}
        self._chosen: set[int] | None = None
        self.picker = DayPicker(self)
        self.picker.changed.connect(self._on_picked)
        self.picker.closed.connect(self._on_closed)
        self._settle = QTimer(self)
        self._settle.setSingleShot(True)
        self._settle.setInterval(180)
        self._settle.timeout.connect(self._emit)

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), 110), hint.height())

    def sizeHint(self) -> QSize:
        # Wide enough for the longest label it shows, so it doesn't jump as days are picked.
        hint = super().sizeHint()
        widest = max(QFontMetrics(self.font()).horizontalAdvance(s)
                     for s in ("All days", "88 of 888 days", "28 Sep – 30 Sep", "No days"))
        return QSize(max(hint.width(), widest + 48), hint.height())

    def set_days(self, counts: dict[int, tuple[int, int]], chosen: set[int] | None) -> None:
        """The subreddit's saved days and its stored choice. While the calendar is open or a pick is still
        settling, only the days are updated (a running fetch adds some): the user's own pick stands."""
        self.counts = dict(counts)
        if not (self.picker.isVisible() or self._settle.isActive()):
            self._chosen = chosen
        if self.picker.isVisible():
            self.picker.set_days(counts, self._chosen)
        self.setItemText(0, choice_text(self._chosen, counts))
        self.setEnabled(bool(counts))

    def chosen(self) -> set[int] | None:
        return self._chosen

    def showPopup(self) -> None:
        if not self.counts:
            return
        self.picker.set_days(self.counts, self._chosen)
        self.picker.show_latest()
        self.picker.place_under(self)
        self.picker.show()
        self.picker.calendar.setFocus()
        self.update()

    def hidePopup(self) -> None:
        self.picker.hide()

    def _on_picked(self) -> None:
        picked = self.picker.chosen()
        self._chosen = None if set(self.counts) <= picked else picked
        self.setItemText(0, choice_text(self._chosen, self.counts))
        self._settle.start()

    def _on_closed(self) -> None:
        if self._settle.isActive():
            self._settle.stop()
            self._emit()
        self.update()

    def _emit(self) -> None:
        self.chosen_changed.emit(self._chosen)
