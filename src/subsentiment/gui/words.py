"""The word cloud: the words the shown posts and comments use most, sized by how many use them and coloured
by which way those lean (the timeline's blue for positive, orange for negative).

``WordTally`` keeps the counts for the rows on screen. Counting a big subreddit's words from scratch takes
about a second per 100,000 comments, so that runs a slice at a time between frames while the window stays
usable; each row's words are remembered, so moving the span only counts the days it gained or lost. Rows a
fetch adds are counted in as they arrive, as the overview's numbers are.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPixmap
from PySide6.QtWidgets import QToolTip, QWidget

from subsentiment.analysis import WordCounts
from subsentiment.gui import theme
from subsentiment.gui.charts import palette
from subsentiment.gui.worker import Slices

KINDS = ("posts", "comments")
_CELL = 2  # px; the word cloud's grid of taken space
_FONTS: dict[int, object] = {}  # the cloud's font by pixel size


class WordTally(QObject):
    """The word counts of the posts and comments on screen. ``count`` starts over from a new set of rows;
    ``add`` counts in rows a fetch saved (new, or saved again with a new score), and the window redraws the
    cloud with the rest of the overview. ``ready`` is False while a big recount runs between frames, and
    ``changed`` fires when one is done."""

    changed = Signal()

    THREAD_FROM = 2000  # rows to count (or take off) at once; more are counted a slice at a time
    SLICE = 100  # rows counted between two frames: a few milliseconds, even for rows not split into words yet
    SEEN_MAX = 400_000  # rows whose words are remembered, across subreddits; about 250 bytes each

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.counts = {k: WordCounts(k) for k in KINDS}
        self.ready = True
        self._pending: dict[str, list] = {k: [] for k in KINDS}  # rows added while a recount runs
        self._seen = {k: {} for k in KINDS}  # {id: (text, words)}: each row's text is split into words once
        self._slices = Slices(self)
        self._version = 0  # counts every change to the counts, for top()
        self._top, self._top_for = [], None

    def count(self, posts: list, comments: list) -> None:
        """Count these rows' words instead. Rows the counts have already are kept, so moving the span a few days
        only counts the days it gained and takes off the ones it left."""
        self._slices.stop()
        self._pending = {k: [] for k in KINDS}  # rows added for an earlier count's rows don't carry over
        rows = {"posts": list(posts), "comments": list(comments)}
        if sum(map(len, self._seen.values())) > self.SEEN_MAX:
            self._seen = {k: {} for k in KINDS}
        if self.ready:
            ids = {k: {r["id"] for r in rows[k]} for k in KINDS}
            gone = {k: self.counts[k].ids() - ids[k] for k in KINDS}
            new = {k: [r for r in rows[k] if r["id"] not in self.counts[k].ids()] for k in KINDS}
            if sum(len(gone[k]) + len(new[k]) for k in KINDS) < self.THREAD_FROM:
                for k in KINDS:
                    self.counts[k].remove(gone[k])
                    self.counts[k].add(new[k])
                self._version += 1
                self.changed.emit()
                return
        counts = {k: WordCounts(k, (), self._seen[k]) for k in KINDS}
        if sum(map(len, rows.values())) < self.THREAD_FROM:
            for k in KINDS:
                counts[k].add(rows[k])
            self._on_counted(counts)
            return
        self.ready = False
        self._slices.start((counts[k].add(rows[k][i:i + self.SLICE]) for k in KINDS
                            for i in range(0, len(rows[k]), self.SLICE)), lambda: self._on_counted(counts))

    def _on_counted(self, counts: dict) -> None:
        for k in KINDS:
            counts[k].add(self._pending[k])
            self._pending[k] = []
        self.counts, self.ready = counts, True
        self._version += 1
        self.changed.emit()

    def add(self, kind: str, rows: list) -> None:
        if not rows:
            return
        if not self.ready:
            self._pending[kind].extend(rows)
            return
        self.counts[kind].add(rows)
        self._version += 1

    def top(self, kinds, n: int = 100) -> list[tuple[str, int, float]]:
        """The ``n`` words used most, worked out again only once the counts change."""
        key = (tuple(kinds), n, self._version)
        if key != self._top_for:
            self._top, self._top_for = WordCounts.top(*[self.counts[k] for k in kinds], n=n), key
        return self._top


class WordCloud(QWidget):
    """Words placed outward from the middle along a spiral, the most used first and biggest, each where it
    overlaps none placed before it. Words that don't fit are left out. Hovering one says how many items use
    it and how they split."""

    MIN_PX, MAX_PX = 11.0, 54.0  # font sizes of the least and most used word, at most

    def __init__(self, empty: str = "No words yet", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.empty = empty
        self.busy = False
        self._words: list[tuple[str, int, float]] = []
        self._placed: list[tuple[QRectF, str, int, float, float]] = []  # box, word, items, lean, pixel size
        self._placed_for = None
        self._placing = None  # the words still to place, a few each frame (_place)
        self._slices = Slices(self)
        self._drawn: QPixmap | None = None  # the words as last painted, and what for
        self._drawn_for = None

    def set_words(self, words: list[tuple[str, int, float]], busy: bool = False) -> None:
        words = list(words)
        if (words, busy) == (self._words, self.busy):
            return  # placed already
        self._words, self.busy = words, busy
        self._placed_for = None
        self.update()

    def placed(self) -> list[tuple[QRectF, str, int, float, float]]:
        """Where each shown word is drawn (for tests and the tooltip), placing any still to place."""
        self._layout()
        if self._placing is not None:
            self._slices.stop()
            for _ in self._placing:
                pass
            self._placing = None
        return self._placed

    def _layout(self) -> None:
        """Start placing the words afresh if they or the size changed; they go down a few each frame, the
        biggest first, so a new cloud grows out from the middle instead of holding up the window."""
        key = (self.width(), self.height(), id(self._words))
        if self._placed_for != key:
            self._placed, self._placed_for = [], key
            self._placing = self._place()
            self._slices.start(self._placing, self._placed_all)

    def _placed_all(self) -> None:
        self._placing = None
        self.update()

    def _place(self):
        """Put each word's box in self._placed, yielding after each. Boxes are tried along the spiral against a
        grid of 2 px cells (a bit mask per row of them), so a try costs a few integer ANDs however many words
        are down already."""
        if not self._words:
            return
        w, h = self.width(), self.height()
        most, least = self._words[0][1], self._words[-1][1]
        biggest = min(self.MAX_PX, max(self.MIN_PX + 4, h / 6))
        cx, cy = w / 2, h / 2
        aspect = w / max(h, 1)  # the spiral is stretched to the widget's shape, so the cloud fills it
        reach = max(w, h) / min(aspect, 1)  # past this radius the spiral has left the widget on every side
        spiral = [(1.6 * a * math.cos(a) * aspect, 1.6 * a * math.sin(a))
                  for a in (step * 0.35 for step in range(3000)) if 1.6 * a <= reach]
        rows = [0] * (int(h) // _CELL + 2)  # the cells taken, a row of them per int
        for word, n, lean in self._words:
            share = (math.sqrt(n) - math.sqrt(least)) / ((math.sqrt(most) - math.sqrt(least)) or 1)
            px = self.MIN_PX + (biggest - self.MIN_PX) * share
            m = QFontMetricsF(self._font(px))
            bw, bh = m.horizontalAdvance(word) + px * 0.35, m.height() * 0.92
            if bw > w or bh > h:
                continue
            for dx, dy in spiral:
                x, y = cx + dx - bw / 2, cy + dy - bh / 2
                if x < 0 or y < 0 or x + bw > w or y + bh > h:
                    continue
                c0 = int(x) // _CELL
                mask = ((1 << (int(x + bw) // _CELL - c0 + 1)) - 1) << c0
                taken = range(int(y) // _CELL, int(y + bh) // _CELL + 1)
                if not any(rows[r] & mask for r in taken):
                    for r in taken:
                        rows[r] |= mask
                    self._placed.append((QRectF(x, y, bw, bh), word, n, lean, px))
                    self.update()
                    break
            yield

    @staticmethod
    def _font(px: float):
        size = max(1, round(px))
        if size not in _FONTS:
            _FONTS[size] = theme.display_font(9, 600)
            _FONTS[size].setPixelSize(size)
        return _FONTS[size]

    @staticmethod
    def _color(lean: float, colors: dict) -> QColor:
        """Blue for words that lean positive, orange for negative, fainter the less they lean; the text colour
        for words used about as much either way."""
        if abs(lean) < 0.15:
            return QColor(colors["text"])
        c = QColor(colors["positive" if lean > 0 else "negative"])
        c.setAlphaF(min(1.0, 0.55 + abs(lean)))
        return c

    def paintEvent(self, event) -> None:
        self._layout()
        colors = palette()
        p = QPainter(self)
        if not self._placed:
            if self._placing is None:
                p.setPen(QColor(colors["muted"]))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Counting words…" if self.busy else self.empty)
            return
        # Kept as a picture: a crossfade or the window's tint repaints the cloud without drawing every word again.
        # Words placed since the last paint are added to it, so a growing cloud draws each word once.
        dpr = self.devicePixelRatioF()
        key = (self._placed_for, dpr, tuple(colors.values()))
        if self._drawn_for is None or self._drawn_for[0] != key:
            self._drawn = QPixmap(self.size() * dpr)
            self._drawn.setDevicePixelRatio(dpr)
            self._drawn.fill(Qt.GlobalColor.transparent)
            self._drawn_for = (key, 0)
        done = self._drawn_for[1]
        if done < len(self._placed):
            q = QPainter(self._drawn)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            for box, word, n, lean, px in self._placed[done:]:
                q.setFont(self._font(px))
                q.setPen(self._color(lean, colors))
                q.drawText(box, Qt.AlignmentFlag.AlignCenter, word)
            q.end()
            self._drawn_for = (key, len(self._placed))
        p.drawPixmap(0, 0, self._drawn)

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            hit = next((b for b in self.placed() if b[0].contains(QPointF(e.pos()))), None)
            if hit is None:
                QToolTip.hideText()
            else:
                _, word, n, lean, _ = hit
                way = "leans positive" if lean > 0.1 else "leans negative" if lean < -0.1 else "mixed"
                QToolTip.showText(e.globalPos(), f"{word}: in {n:,} {'post or comment' if n == 1 else 'posts and comments'}"
                                                 f", {way}", self)
            return True
        return super().event(e)
