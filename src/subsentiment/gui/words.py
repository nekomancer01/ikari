"""The word cloud: the words the shown posts and comments use most, sized by how many use them and coloured
by which way those lean (the timeline's blue for positive, orange for negative).

``WordTally`` keeps the counts for the rows on screen. Counting a big subreddit's words from scratch takes
about a second per 100,000 comments, so that runs on a thread while the window stays usable; rows a fetch adds
are counted in as they arrive, as the overview's numbers are.
"""
from __future__ import annotations

import math
import threading

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QPainter
from PySide6.QtWidgets import QToolTip, QWidget

from subsentiment.analysis import WordCounts
from subsentiment.gui import theme
from subsentiment.gui.charts import palette

KINDS = ("posts", "comments")


class _Relay(QObject):
    """Carries a finished count from its thread to the GUI thread. It lives as long as the app, so a count
    that ends after its window closed has somewhere safe to go."""
    counted = Signal(object, int, object)  # tally id, recount number, {kind: WordCounts}


_relay: _Relay | None = None


class WordTally(QObject):
    """The word counts of the posts and comments on screen. ``count`` starts over from a new set of rows;
    ``add`` counts in rows a fetch saved (new, or saved again with a new score), and the window redraws the
    cloud with the rest of the overview. ``ready`` is False while a recount runs on its thread, and
    ``changed`` fires when one is done."""

    changed = Signal()

    THREAD_FROM = 5000  # rows; fewer are counted at once

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.counts = {k: WordCounts(k) for k in KINDS}
        self.ready = True
        self._recount = 0
        self._pending: dict[str, list] = {k: [] for k in KINDS}  # rows added while a recount runs
        global _relay
        _relay = _relay or _Relay()
        _relay.counted.connect(self._on_relayed)  # dropped by Qt when this tally goes

    def count(self, posts: list, comments: list) -> None:
        self._recount += 1
        rows = {"posts": list(posts), "comments": list(comments)}
        if len(rows["posts"]) + len(rows["comments"]) < self.THREAD_FROM:
            self._on_counted(self._recount, {k: WordCounts(k, rows[k]) for k in KINDS})
            return
        self.ready = False
        self._pending = {k: [] for k in KINDS}
        threading.Thread(target=self._count, args=(self._recount, rows), daemon=True).start()

    def _count(self, recount: int, rows: dict) -> None:
        _relay.counted.emit(id(self), recount, {k: WordCounts(k, rows[k]) for k in KINDS})

    def _on_relayed(self, tally, recount: int, counts: dict) -> None:
        if tally == id(self):
            self._on_counted(recount, counts)

    def _on_counted(self, recount: int, counts: dict) -> None:
        if recount != self._recount:  # other rows were chosen meanwhile; their count is on its way
            return
        for k in KINDS:
            counts[k].add(self._pending[k])
            self._pending[k] = []
        self.counts, self.ready = counts, True
        self.changed.emit()

    def add(self, kind: str, rows: list) -> None:
        if not rows:
            return
        if not self.ready:
            self._pending[kind].extend(rows)
            return
        self.counts[kind].add(rows)

    def top(self, kinds, n: int = 100) -> list[tuple[str, int, float]]:
        return WordCounts.top(*[self.counts[k] for k in kinds], n=n)


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

    def set_words(self, words: list[tuple[str, int, float]], busy: bool = False) -> None:
        self._words, self.busy = list(words), busy
        self._placed_for = None
        self.update()

    def placed(self) -> list[tuple[QRectF, str, int, float, float]]:
        """Where each shown word is drawn (for tests and the tooltip)."""
        key = (self.width(), self.height(), id(self._words))
        if self._placed_for != key:
            self._placed, self._placed_for = self._place(), key
        return self._placed

    def _place(self) -> list:
        if not self._words:
            return []
        w, h = self.width(), self.height()
        most, least = self._words[0][1], self._words[-1][1]
        biggest = min(self.MAX_PX, max(self.MIN_PX + 4, h / 6))
        done: list = []
        cx, cy = w / 2, h / 2
        aspect = w / max(h, 1)  # the spiral is stretched to the widget's shape, so the cloud fills it
        for word, n, lean in self._words:
            share = (math.sqrt(n) - math.sqrt(least)) / ((math.sqrt(most) - math.sqrt(least)) or 1)
            px = self.MIN_PX + (biggest - self.MIN_PX) * share
            m = QFontMetricsF(self._font(px))
            bw, bh = m.horizontalAdvance(word) + px * 0.35, m.height() * 0.92
            if bw > w or bh > h:
                continue
            for step in range(3000):
                angle = step * 0.35
                r = 1.6 * angle
                box = QRectF(cx + r * math.cos(angle) * aspect - bw / 2, cy + r * math.sin(angle) - bh / 2, bw, bh)
                if box.left() < 0 or box.top() < 0 or box.right() > w or box.bottom() > h:
                    if r * min(aspect, 1) > max(w, h):  # the spiral has left the widget on every side
                        break
                    continue
                if not any(box.intersects(b[0]) for b in done):
                    done.append((box, word, n, lean, px))
                    break
        return done

    @staticmethod
    def _font(px: float):
        f = theme.display_font(9, 600)
        f.setPixelSize(max(1, round(px)))
        return f

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
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        colors = palette()
        placed = self.placed()
        if not placed:
            p.setPen(QColor(colors["muted"]))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Counting words…" if self.busy else self.empty)
            return
        for box, word, n, lean, px in placed:
            p.setFont(self._font(px))
            p.setPen(self._color(lean, colors))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, word)

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
