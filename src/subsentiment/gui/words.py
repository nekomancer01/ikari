"""The word cloud: the words the shown posts and comments use most, sized by how many use them and coloured
by which way those lean (the timeline's blue for positive, orange for negative).

``WordTally`` keeps the counts for the rows on screen. Counting a big subreddit's words from scratch takes
about a second per 100,000 comments, so that runs a slice at a time between frames while the window stays
usable; each row's words are remembered, so moving the span only counts the days it gained or lost. Rows a
fetch adds are counted in as they arrive, as the overview's numbers are.

Clicking a word picks it in ``WordMeanings``, beside the cloud on the Words page, where a slider sets what it
means in the subreddit shown, from negative through neutral to positive (sentiment.Lexicon). ``Rescore`` then
scores the saved posts and comments using it again, a slice at a time, and every page follows as it goes.
"""
from __future__ import annotations

import math
import time
from collections import Counter
from itertools import islice

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QRectF, QSize, QSizeF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLineEdit, QSizePolicy, QSlider, QToolTip, QVBoxLayout, QWidget,
)

from subsentiment.analysis import WordCounts
from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
from subsentiment.gui.days import flat_button
from subsentiment.gui.widgets import ElidedLabel, VerticalScrollArea, label
from subsentiment.gui.worker import BackgroundJob, LoadWorker, ScorePool, Slices
from subsentiment.sentiment import Lexicon
from subsentiment.store import column

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
    LOOK = 20_000  # rows gone through at once to find what a moved span gained and lost; more, a slice at a time

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.known: dict[str, dict] = {k: {} for k in KINDS}  # id -> words of rows split elsewhere (learn)
        self.counts = {k: WordCounts(k, known=self.known[k]) for k in KINDS}
        self.ready = True
        self._moving = False  # working out what a moved span gained and lost, a slice at a time
        self._pending: dict[str, list] = {k: [] for k in KINDS}  # rows added while a recount or a move runs
        self._slices = Slices(self)
        self._version = 0  # counts every change to the counts, for top()
        self._top, self._top_for = [], None

    def busy(self) -> bool:
        """A recount, or a moved span's change, is being worked out between frames."""
        return not self.ready or self._slices.running()

    def learn(self, known: dict[str, dict]) -> None:
        """The words of rows split elsewhere ({kind: {id: words}}), for rows read without theirs."""
        self.known = {k: known.get(k) or {} for k in KINDS}
        for k in KINDS:
            self.counts[k].known = self.known[k]

    def hold(self) -> None:
        """Not ready until the next count: the rows to count are on their way (their words being split)."""
        self._slices.stop()
        self._moving, self.ready = False, False
        self._pending = {k: [] for k in KINDS}
        self._version += 1
        self.changed.emit()

    def count(self, posts: list, comments: list, fresh: bool = False) -> None:
        """Count these rows' words instead. Rows the counts have already are kept, so moving the span a few days
        only counts the days it gained and takes off the ones it left; ``fresh`` (rows that may have been saved
        again with a new label or text) counts them all again. Working out what a moved span gained and lost
        goes through every row of it, so with a big subreddit that's done a slice at a time too, the counts
        shown meanwhile."""
        self._slices.stop()
        self._moving = False
        self._pending = {k: [] for k in KINDS}  # rows added for an earlier count's rows don't carry over
        rows = {"posts": list(posts), "comments": list(comments)}
        if self.ready and not fresh:
            if sum(map(len, rows.values())) < self.LOOK:
                for _ in self._moved(rows, sliced=False):
                    pass
            else:
                self._moving = True  # rows added meanwhile wait, so the counts and ids looked through hold still
                self._slices.start(self._moved(rows, sliced=True), lambda: None)
            return
        self._recount(rows)

    def _moved(self, rows: dict, sliced: bool):
        """Count in the rows not counted yet and take off the ones counted that aren't in ``rows``, yielding as
        it goes through them; a big change is counted afresh instead (in these slices, when ``sliced``)."""
        gone, new = {}, {}
        for k in KINDS:
            counted, ids, look = self.counts[k].ids(), set(), self.LOOK
            for i in range(0, len(rows[k]), look):
                ids.update(column(rows[k][i:i + look], "id"))
                yield
            gone[k], was = [], iter(counted)
            while chunk := list(islice(was, look)):
                gone[k] += [rid for rid in chunk if rid not in ids]
                yield
            new[k] = []
            for i in range(0, len(rows[k]), look):
                new[k] += [r for r in rows[k][i:i + look] if r["id"] not in counted]
                yield
        self._moving = False
        if sum(len(gone[k]) + len(new[k]) for k in KINDS) < self.THREAD_FROM:
            for k in KINDS:
                self.counts[k].remove(gone[k])
                self.counts[k].add(new[k] + self._pending[k])
                self._pending[k] = []
            self._version += 1
            self.changed.emit()
        elif sliced:  # counted afresh a slice at a time: about as quick, and the counts shown stay whole meanwhile
            yield from self._recounting(rows)
        else:
            self._recount(rows)

    def _recount(self, rows: dict) -> None:
        if sum(map(len, rows.values())) < self.THREAD_FROM:
            counts = {k: WordCounts(k, known=self.known[k]) for k in KINDS}
            for k in KINDS:
                counts[k].add(rows[k])
            self._on_counted(counts)
        else:
            self._slices.start(self._recounting(rows), lambda: None)

    def _recounting(self, rows: dict):
        """Not ready from now; the steps that count ``rows`` afresh, a slice each."""
        counts = {k: WordCounts(k, known=self.known[k]) for k in KINDS}
        self.ready = False

        def steps():
            for k in KINDS:
                for i in range(0, len(rows[k]), self.SLICE):
                    counts[k].add(rows[k][i:i + self.SLICE])
                    yield
            self._on_counted(counts)
        return steps()

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
        if not self.ready or self._moving:
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


class _Word:
    """A word in the cloud: where it's headed (its box and pixel size, ``shown`` 1, or 0 on its way out) and
    where it's drawn now (centre, size, alpha and tint), eased there from where it was when it got the target."""

    __slots__ = ("word", "n", "lean", "box", "px", "shown", "x", "y", "size", "alpha", "tint", "start", "was")

    def __init__(self, word: str, box: QRectF, px: float):
        self.word, self.box, self.px, self.shown = word, box, px, 1.0
        c = box.center()
        self.x, self.y, self.size, self.alpha, self.tint = c.x(), c.y(), px * WordCloud.GROW_FROM, 0.0, 1.0
        self.start = (self.x, self.y, self.size, self.alpha, self.tint)
        self.lean = self.was = 0.0  # its lean, and the one it had when it got its target (a colour fades across)

    def ease(self, move: float, grow: float) -> None:
        """Step from where the word was to its target: ``move`` of the way for its place and colour, ``grow``
        for its size (which overshoots a little, so a word arriving or growing pops)."""
        x0, y0, s0, a0, t0 = self.start
        c = self.box.center()
        self.x, self.y = x0 + (c.x() - x0) * move, y0 + (c.y() - y0) * move
        self.size = s0 + (self.px * (1 if self.shown else WordCloud.GROW_FROM) - s0) * grow
        self.alpha = a0 + (self.shown - a0) * move
        self.tint = t0 + (WordCloud.hue(self.lean)[1] - t0) * move


class WordCloud(QWidget):
    """Words placed outward from the middle along a spiral, the most used first and biggest, each where it
    overlaps none placed before it. Words that don't fit are left out. Hovering one says how many items use
    it and how they split.

    The cloud keeps its shape as the words change, as live word clouds do (Cui et al.'s context-preserving
    clouds, d3-cloud's enter/update/exit): a word already shown is tried where it is first, then ever further
    out from there, so it stays put unless something bigger needs its place; new words go in from the middle.
    Then everything eases to the new layout: words slide, grow and shrink, new ones pop in and dropped ones
    fade. Each frame of that draws a prepared picture of each word, scaled; at rest the cloud is one picture."""

    MIN_PX, MAX_PX = 11.0, 54.0  # font sizes of the least and most used word, at most
    MOVE_MS = 650  # a change easing in
    GROW_FROM = 0.4  # of its size, where a word pops in from and fades out to
    BUSY_DIM = 0.45  # how far the cloud fades while new counts are worked out
    PULL = 0.06  # of the way to the middle, a step a word takes each update where that's free
    FAR = 0.3  # of the widget's shorter side: a word moved further fades out and in rather than sliding
    RESHAPE = 0.15  # a change of width or height up to this share keeps the words where they are

    word_clicked = Signal(str)

    def __init__(self, empty: str = "No words yet", parent=None):
        super().__init__(parent)
        self.setMinimumHeight(120)
        self.setMouseTracking(True)
        self.empty = empty
        self.busy = False
        self._words: list[tuple[str, int, float]] = []
        self.meant: dict[str, float] = {}  # the words shown by their set meaning (set_words)
        self._version = 0  # counts every change to the words
        self._placed: list[tuple[QRectF, str, int, float, float]] = []  # box, word, items, lean, pixel size
        self._shown: dict = {}  # every word drawn: those on their way out first (by id), then the rest (by word)
        self._placed_for = None  # (width, height, version) of the words placed
        self._placing = None  # the words being placed, a few each frame (_place), and what for
        self._placing_for = None
        self._next: list[tuple[QRectF, str, int, float, float]] = []
        self._slices = Slices(self)
        self._spiral, self._spiral_for = [], None
        self._painted = False  # painted since last shown: a change can ease in from what's on screen
        self._anim = QVariantAnimation(self, duration=self.MOVE_MS, startValue=0.0, endValue=1.0)
        self._anim.valueChanged.connect(self._step)
        self._anim.finished.connect(self._settled)
        self._move, self._grow = QEasingCurve(QEasingCurve.Type.OutCubic), QEasingCurve(QEasingCurve.Type.OutBack)
        self._grow.setOvershoot(1.2)
        self._dim = 1.0
        self._moved = 1.0  # how far the change easing in has got
        self._dimming = motion.Tween(lambda v: (setattr(self, "_dim", v), self.update()), self, motion.VIEW_MS)
        self._sprites: dict[tuple, QPixmap] = {}  # each word's picture, by word, size, colour and pixel ratio
        self._drawn: QPixmap | None = None  # the words at rest, and what for
        self._drawn_for = None
        self._next_drawn = (None, None)  # the same for the words being placed

    def set_words(self, words: list[tuple[str, int, float]], busy: bool = False,
                  meant: dict[str, float] | None = None) -> None:
        """Show these words: faded while ``busy`` (new counts are on their way). ``meant`` ({word: -1..1}) are
        the words the user set a meaning for here, which hovering one tells."""
        self.meant = meant or {}
        words = list(words)
        if (words, busy) == (self._words, self.busy):
            return  # placed already
        if words != self._words:
            self._words, self._version = words, self._version + 1
        self.busy = busy
        self._dimming.set(self.BUSY_DIM if busy and words else 1.0, animate=self._painted)
        self.update()

    def placed(self) -> list[tuple[QRectF, str, int, float, float]]:
        """Where each shown word is drawn (for tests and the tooltip), placing any still to place."""
        self._layout()
        while self._placing is not None:
            self._slices.stop()
            for _ in self._placing:
                pass
            self._placed_all()
            self._layout()
        return self._placed

    def _layout(self) -> None:
        """Start placing the words if they or the size changed; they go down a few each frame, the biggest
        first, so a new layout never holds up the window. Words changed while one is placed are placed next."""
        key, before = (self.width(), self.height(), self._version), self._placed_for
        if self._placing is None and before != key:
            # A small change of size (a strip appearing above, a window edge dragged) keeps the words where they
            # are, moved with it; a big one lays the cloud out afresh to fill its new shape.
            near = before is not None and all(abs(a - b) <= self.RESHAPE * b for a, b in zip(key[:2], before[:2]))
            self._placing_for, self._next, self._next_drawn = (key, self._painted, near), [], (None, None)
            self._placing = self._place()
            self._slices.start(self._placing, self._placed_all)

    def _placed_all(self) -> None:
        """Ease every word to where it was just placed, from where it is on screen; straight there after a big
        resize or with motion off, and popping in on a page just opened."""
        key, painted, near = self._placing_for
        self._placing, self._placed_for, self._placed = None, key, self._next
        self._drawn, self._drawn_for = self._next_drawn
        # Placed for a cloud that wasn't on screen (a page just opened): the words pop in instead.
        fresh = not (painted and self._painted)
        if fresh:
            self._shown = {}
        ease = motion.enabled() and self.isVisible() and (fresh or near)
        shown, far = {}, self.FAR * min(key[0], key[1])
        for box, word, n, lean, px in self._placed:
            w = self._shown.get(word)
            if w is None or math.hypot(w.x - box.center().x(), w.y - box.center().y()) > far:
                w = _Word(word, box, px)  # one that had to go far fades there and pops in where it goes
            w.was = w.lean if w.alpha else lean
            w.box, w.n, w.lean, w.px, w.shown = box, n, lean, px, 1.0
            shown[word] = w
        # Words on their way out, by id: one that comes back while it fades is a new word.
        leaving = {id(w): w for w in self._shown.values() if shown.get(w.word) is not w and ease and w.alpha > 0.01}
        for w in leaving.values():
            w.shown = 0.0
        self._shown = leaving | shown  # drawn first, so under the words staying
        self._anim.stop()
        for w in self._shown.values():
            w.start = (w.x, w.y, w.size, w.alpha, w.tint)
        if ease:
            self._anim.start()
        else:
            self._step(1.0)
            self._settled()

    def _step(self, t: float) -> None:
        move, grow = self._move.valueForProgress(t), self._grow.valueForProgress(t)
        self._moved = move
        for w in self._shown.values():
            w.ease(move, grow)
        self.update()

    def _settled(self) -> None:
        self._shown = {word: w for word, w in self._shown.items() if w.shown}
        self.update()

    def _place(self):
        """Work out each word's box into self._next, a few words each step. Boxes are tried against a grid of
        2 px cells (a bit mask per row of them), so a try costs a few integer ANDs however many words are down.

        Words already shown keep their place first, the biggest first, wherever nothing bigger has grown into it;
        a new word that's among the biggest goes in with them, as near the middle as it can. The rest are tried
        along a spiral: a word that lost its place out from where it was, so it shifts as little as it can; a
        new word out from the middle. Last, each word takes a small step towards the middle
        where that's free, so gaps left by words that shrank or went close up over a few updates without
        pushing anything else aside."""
        if not self._words:
            return
        if len(self._sprites) > 2000:  # pictures of words long gone, and of sizes they had
            self._sprites.clear()
        w, h = self.width(), self.height()
        most, least = self._words[0][1], self._words[-1][1]
        # Narrow (the Words page beside its meanings card) caps it too, so a word of up to nine letters fits across
        # at the biggest size: words used as often stay the same size, and only longer ones are shrunk below.
        across = QFontMetricsF(self._font(self.MAX_PX)).horizontalAdvance("abcdefghi") / self.MAX_PX + 0.35
        biggest = min(self.MAX_PX, max(self.MIN_PX + 4, min(h / 6, w / across)))
        cx, cy = w / 2, h / 2
        if self._spiral_for != (w, h):
            # Stretched to the widget's shape, so the cloud fills it; out to where it has left the widget wherever
            # it starts. Close steps near the start (small shifts first), wider further out.
            aspect, reach, spiral, a = w / max(h, 1), max(w, h) / min(w / max(h, 1), 1), [], 0.0
            while 1.6 * a <= reach:
                spiral.append((1.6 * a * math.cos(a) * aspect, 1.6 * a * math.sin(a)))
                a += max(0.12, 3 / max(1.6 * a, 1))
            self._spiral, self._spiral_for = spiral, (w, h)
        # Where the words shown are, moved with the widget if its size changed a little (see _layout).
        ow, oh = self._placed_for[:2] if self._placed_for else (w, h)
        was = {word: (s.box.center().x() * w / max(ow, 1), s.box.center().y() * h / max(oh, 1))
               for word, s in self._shown.items() if s.shown and self._placing_for[2]}
        rows = [0] * (int(h) // _CELL + 2)  # the cells taken, a row of them per int

        def free(x, y, bw, bh):
            """The cells a box at x, y takes (mask, rows), or None where it's off the widget or taken."""
            if x < 0 or y < 0 or x + bw > w or y + bh > h:
                return None
            c0 = int(x) // _CELL
            mask = ((1 << (int(x + bw) // _CELL - c0 + 1)) - 1) << c0
            taken = range(int(y) // _CELL, int(y + bh) // _CELL + 1)
            return None if any(rows[r] & mask for r in taken) else (mask, taken)

        sized = []
        for word, n, lean in self._words:
            share = (math.sqrt(n) - math.sqrt(least)) / ((math.sqrt(most) - math.sqrt(least)) or 1)
            px = self.MIN_PX + (biggest - self.MIN_PX) * share
            m = QFontMetricsF(self._font(px))
            bw, bh = m.horizontalAdvance(word) + px * 0.35, m.height() * 0.92
            if bw > w:  # a long word on a narrow cloud: smaller, rather than left out
                px = max(1.0, px * w / bw * 0.95)
                m = QFontMetricsF(self._font(px))
                bw, bh = m.horizontalAdvance(word) + px * 0.35, m.height() * 0.92
            if bw <= w and bh <= h:
                sized.append((word, n, lean, px, bw, bh))
        at = {}  # word: (x, y, the cells it takes)

        def put(word, bw, bh, origin, tries):
            """Put the word at the first free spot of tries (offsets from origin), if there is one."""
            for dx, dy in tries:
                x, y = origin[0] + dx - bw / 2, origin[1] + dy - bh / 2
                cells = free(x, y, bw, bh)
                if cells:
                    for r in cells[1]:
                        rows[r] |= cells[0]
                    at[word] = (x, y, cells)
                    return

        # First the words shown keep their place, and a new word big enough to belong in the middle (one that's
        # suddenly used a lot) takes the free space nearest it, with them; then the rest find room.
        big = (self.MIN_PX + biggest) / 2
        for word, n, lean, px, bw, bh in sized:
            if word in was:
                put(word, bw, bh, was[word], ((0, 0),))
            elif px >= big:
                put(word, bw, bh, (cx, cy), self._spiral)
                yield
        yield
        for word, n, lean, px, bw, bh in sized:
            if word not in at and (word in was or px < big):
                put(word, bw, bh, was.get(word, (cx, cy)), self._spiral)
                yield
        for word, n, lean, px, bw, bh in sized:
            if word not in at:
                continue
            x, y, (mask, taken) = at[word]
            for r in taken:
                rows[r] &= ~mask
            nx, ny = x + (cx - x - bw / 2) * self.PULL, y + (cy - y - bh / 2) * self.PULL
            cells = free(nx, ny, bw, bh)
            if cells:
                x, y, (mask, taken) = nx, ny, cells
            for r in taken:
                rows[r] |= mask
            self._next.append((QRectF(x, y, bw, bh), word, n, lean, px))
        # The pictures the change is drawn with, made here a few at a time rather than in the frame it starts:
        # each word's (in its old colour too, if that changed) and the cloud's at rest.
        colors, dpr = palette(), self.devicePixelRatioF()
        for box, word, n, lean, px in self._next:
            self._sprite(word, px, colors[self.hue(lean)[0]], dpr)
            if word in self._shown:
                self._sprite(word, px, colors[self.hue(self._shown[word].lean)[0]], dpr)
            yield
        steps = self._draw(self._next, colors, dpr)
        picture = next(steps)
        yield from steps
        self._next_drawn = (picture, (self._placing_for[0], dpr, tuple(colors.values())))

    def _draw(self, placed: list, colors: dict, dpr: float):
        """Draw the words at rest into a picture: yields the picture, then once after each word."""
        picture = QPixmap(self.size() * dpr)
        picture.setDevicePixelRatio(dpr)
        picture.fill(Qt.GlobalColor.transparent)
        yield picture
        if picture.isNull():  # no room at all
            return
        q = QPainter(picture)
        q.setRenderHint(QPainter.RenderHint.Antialiasing)
        for box, word, n, lean, px in placed:
            hue, tint = self.hue(lean)
            color = QColor(colors[hue])
            if hue != "text":
                color.setAlphaF(tint)
            q.setFont(self._font(px))
            q.setPen(color)
            q.drawText(box, Qt.AlignmentFlag.AlignCenter, word)
            yield
        q.end()

    @staticmethod
    def _font(px: float):
        size = max(1, round(px))
        if size not in _FONTS:
            _FONTS[size] = theme.display_font(9, 600)
            _FONTS[size].setPixelSize(size)
        return _FONTS[size]

    @staticmethod
    def hue(lean: float) -> tuple[str, float]:
        """Blue for words that lean positive, orange for negative, fainter the less they lean; the text colour
        for words used about as much either way. The palette's colour, and how opaque."""
        if abs(lean) < 0.15:
            return "text", 1.0
        return "positive" if lean > 0 else "negative", min(1.0, 0.55 + abs(lean))

    def _sprite(self, word: str, px: float, color: str, dpr: float) -> tuple[QPixmap, float]:
        """A picture of the word in a colour at the font size it's drawn at when still (drawn scaled while it
        moves), and that size. Made once per word, size and colour, so a frame only draws pictures."""
        px = max(1, round(px))
        key = (word, px, color, dpr)
        if key not in self._sprites:
            font = self._font(px)
            m = QFontMetricsF(font)
            size = QSizeF(m.horizontalAdvance(word) + px * 0.35, m.height())
            sprite = QPixmap((size * dpr).toSize() + QSize(2, 2))
            sprite.setDevicePixelRatio(dpr)
            sprite.fill(Qt.GlobalColor.transparent)
            q = QPainter(sprite)
            q.setRenderHint(QPainter.RenderHint.Antialiasing)
            q.setFont(font)
            q.setPen(QColor(color))
            q.drawText(QRectF(QPointF(0, 0), sprite.deviceIndependentSize()), Qt.AlignmentFlag.AlignCenter, word)
            q.end()
            self._sprites[key] = sprite
        return self._sprites[key], px

    def paintEvent(self, event) -> None:
        self._layout()
        colors = palette()
        p = QPainter(self)
        if self._placing is not None and not self._placing_for[1]:
            return  # the words that were here before it was shown aren't shown again; the new ones pop in shortly
        if not self._shown:
            if self._placing is None:
                p.setPen(QColor(colors["muted"]))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Counting words…" if self.busy else self.empty)
            self._painted = True
            return
        dpr = self.devicePixelRatioF()
        if self._anim.state() == QVariantAnimation.State.Running:
            p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            for w in self._shown.values():
                if w.alpha * w.tint < 0.004:
                    continue
                # A word changing colour: the new one fades in over the old.
                hue, was = self.hue(w.lean)[0], self.hue(w.was)[0]
                for color, share in ((colors[hue], 1.0),) if hue == was else (
                        (colors[was], 1.0), (colors[hue], self._moved)):
                    sprite, px = self._sprite(w.word, w.px, color, dpr)
                    size = sprite.deviceIndependentSize() * (w.size / px)
                    p.setOpacity(self._dim * w.alpha * w.tint * share)
                    p.drawPixmap(QRectF(w.x - size.width() / 2, w.y - size.height() / 2, size.width(), size.height()),
                                 sprite, QRectF(sprite.rect()))
            self._painted = True
            return
        # At rest, kept as a picture: a crossfade or the window's tint repaints the cloud without drawing every
        # word again.
        key = (self._placed_for, dpr, tuple(colors.values()))
        if self._drawn_for != key:  # drawn as the words were placed, unless the theme or screen changed since
            steps = self._draw(self._placed, colors, dpr)
            self._drawn, self._drawn_for = next(steps), key
            for _ in steps:
                pass
        p.setOpacity(self._dim)
        p.drawPixmap(0, 0, self._drawn)
        self._painted = True

    def hideEvent(self, event) -> None:
        self._painted = False  # shown again, it starts from its new words rather than easing from old ones
        super().hideEvent(event)

    def word_at(self, pos: QPointF) -> str | None:
        return next((b[1] for b in self._placed if b[0].contains(pos)), None)

    def mouseMoveEvent(self, e) -> None:
        on = self.word_at(e.position()) is not None
        self.setCursor(Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, e) -> None:
        word = self.word_at(e.position()) if e.button() == Qt.MouseButton.LeftButton else None
        if word is not None:
            self.word_clicked.emit(word)

    def event(self, e) -> bool:
        if e.type() == QEvent.Type.ToolTip:
            hit = next((b for b in self.placed() if b[0].contains(QPointF(e.pos()))), None)
            if hit is None:
                QToolTip.hideText()
            else:
                _, word, n, lean, _ = hit
                way = "leans positive" if lean > 0.1 else "leans negative" if lean < -0.1 else "mixed"
                if word in self.meant:  # (its colour is still how the items using it were scored)
                    way += f". You set it to {meaning(self.meant[word]).lower()} here; click to change it"
                else:
                    way += ". Click to set what it means here"
                QToolTip.showText(e.globalPos(), f"{word}: in {n:,} {'post or comment' if n == 1 else 'posts and comments'}"
                                                 f", {way}", self)
            return True
        return super().event(e)


def meaning(weight: float) -> str:
    """How a word set to ``weight`` reads, in words."""
    if weight == 0:
        return "Neutral"
    way = "positive" if weight > 0 else "negative"
    return f"Slightly {way}" if abs(weight) < 0.35 else f"Very {way}" if abs(weight) > 0.7 else way.capitalize()


class Rescore(QObject):
    """Scores a subreddit's saved posts and comments again by its words as they're set now, once some words'
    meanings there changed: only the items using one of those words. They're the rows the window has of it, or
    else read on a thread (the window's LoadWorker, which goes through the day packs). They're searched for the
    words a slice at a time between frames, and what's found is scored by a ScorePool in other processes, a
    chunk at a time, or here between frames when there's little of it or the processes can't run. Each is saved
    as it's scored. Every quarter second what's done is committed and sent out with ``rescored``, so the pages
    follow as it goes. A change while one runs starts it again with every word it was doing."""

    rescored = Signal(str, str, object)  # subreddit (lowercased), "posts" or "comments", the rows with new scores
    progress = Signal(str, int, int)  # subreddit, thousandths done, 1000
    finished = Signal(str)  # subreddit: every item using the words is scored again and saved
    # Just before finished: subreddit, the words, how many items use them, how many labels the words changed
    # each way ({(label without them, label with them): items}), and how many others' scores were only out of
    # date (scored by an older version).
    scored = Signal(str, object, int, object, int)
    failed = Signal(str, str)  # subreddit, what went wrong reading its items

    BATCH = 0.25  # seconds between two batches sent out
    COMMIT = 0.05  # seconds between two commits: other connections (a fetch) wait while one is open
    CHUNK = 200  # items found sent to the processes at once; fewer than this in all are scored here
    SCORE_COST = 400  # scoring an item found takes about as long as searching this many, for the progress

    def __init__(self, store, db_path: str, parent: QObject | None = None):
        super().__init__(parent)
        self.store, self.db_path = store, db_path
        self.job = BackgroundJob(self)
        self.job.ended.connect(self._next)
        self._slices = Slices(self)
        self.pool = ScorePool(self)
        self.pool.scored.connect(self._pooled)
        self.pool.failed.connect(self._unpooled)
        self._todo: dict[str, set[str]] = {}  # subreddit -> words to rescore for, waiting their turn
        self._doing: tuple[str, set[str]] | None = None
        self._rows: dict[str, dict | None] = {}  # what start was given to go through, by subreddit
        self._run = 0  # counts runs, so a read or chunk for one dropped since is ignored
        self._batch: dict[str, list] = {k: [] for k in KINDS}  # rescored, not saved yet
        # By subreddit, the items its run found using the words, those whose label the words change (without
        # them, with them), and those it gave a new label for another reason (scored by an older version): kept
        # when a run starts again with more words, so the end tells everything it did.
        self._tally: dict[str, tuple[set, dict, set]] = {}
        self._out = 0  # chunks the run has in the processes
        self._retry: list[list] = []  # chunks they couldn't score, scored here once the search is done
        self._lexicon: tuple[dict, frozenset] = ({}, frozenset())  # the run's weights, and the words it's for
        self._counts = [0, 0, 0, 0]  # the run's items to search, searched, found and scored
        self._shown = 0  # how far it's got, in thousandths, as last sent
        self._sent_at = self._committed_at = 0.0

    def start(self, subreddit: str, words, rows: dict | None = None) -> None:
        """Rescore for ``words``; ``rows`` ({kind: rows}) are every saved post and comment of it, when the
        window has them all, so they needn't be read again."""
        sub = subreddit.lower()
        words = {Lexicon.key(w) for w in words}
        if self._doing is not None and self._doing[0] == sub:
            words |= self._doing[1]
            self._drop()
        self._todo[sub] = self._todo.get(sub, set()) | words
        self._rows[sub] = None if rows is None else {k: list(v) for k, v in rows.items()}  # as they are now
        self._next()

    def running(self, subreddit: str | None = None) -> bool:
        doing = [self._doing[0]] if self._doing else []
        return any(subreddit is None or s == subreddit.lower() for s in [*doing, *self._todo])

    def stop(self) -> None:
        """Save what's been rescored and stop (the window is closing); its read's thread is waited for."""
        self._todo.clear()
        self._rows.clear()
        self._drop()
        self.pool.close()
        if self.job.busy:
            self.job.worker.finished.disconnect()
            self.job.worker.failed.disconnect()
            self.job.worker.stop()
            self.job.qthread.quit()
            self.job.qthread.wait(3000)

    def _drop(self) -> None:
        self._slices.stop()
        if self._doing is not None:
            self._save(self._doing[0])
        self._doing = None
        self._run += 1
        if self._out:
            self.pool.close()  # what's queued there is for the run dropped
        self._out, self._retry = 0, []

    def _next(self) -> None:
        if self._doing is not None or self.job.busy or not self._todo:
            return
        sub = next(iter(self._todo))
        self._doing = (sub, self._todo.pop(sub))
        self._run += 1
        rows = self._rows.pop(sub, None)
        if rows is not None:
            self._on_read((self._run, rows, None))
            return
        worker = LoadWorker(self.db_path, sub, (None, None), self._run, words=False)  # every row, whatever the window filters
        worker.read.connect(self._on_read)
        worker.failed.connect(lambda message, sub=sub: (self._drop(), self._tally.pop(sub, None),
                                                        self.failed.emit(sub, message)))
        self.job.start(worker)

    def _on_read(self, read: tuple) -> None:
        run, rows, _ = read
        if run == self._run and self._doing is not None:
            self._slices.start(self._search(*self._doing, rows), self._settle)

    def _search(self, sub: str, words: set[str], rows: dict):
        """Find the items using ``words``, yielding after each, and send them to be scored a chunk at a time."""
        self._lexicon = (self.store.lexicon(sub).weights, frozenset(words))
        uses = Lexicon(dict.fromkeys(words, 0.0)).uses
        self._counts, self._shown = [sum(map(len, rows.values())), 0, 0, 0], 0
        self._sent_at = self._committed_at = time.perf_counter()
        chunk, sent = [], False
        for kind in KINDS:
            for r in rows[kind]:
                text = (r["body"] or "") if kind == "comments" else f"{r['title'] or ''}\n\n{r['selftext'] or ''}".strip()
                self._counts[1] += 1
                if uses(text):
                    self._counts[2] += 1
                    chunk.append((kind, r, text))
                    if len(chunk) == self.CHUNK:
                        self._submit(sub, chunk)
                        chunk, sent = [], True
                self._tick(sub)
                yield
        if sent and chunk:
            self._submit(sub, chunk)
        elif chunk:
            yield from self._score_here(sub, [chunk])

    def _submit(self, sub: str, chunk: list) -> None:
        self._out += 1
        self.pool.submit((self._run, sub, chunk), *self._lexicon, [t for *_, t in chunk])

    def _score_here(self, sub: str, chunks: list[list]):
        for chunk in chunks:
            for item in chunk:
                self._apply(sub, [item], Lexicon.compare(*self._lexicon, [item[2]]))
                yield

    def _pooled(self, tag, results: list) -> None:
        run, sub, chunk = tag
        if run == self._run:
            self._out -= 1
            self._apply(sub, chunk, results)
            self._settle()

    def _unpooled(self, tag) -> None:
        run, sub, chunk = tag
        if run == self._run:
            self._out -= 1
            self._retry.append(chunk)
            self._settle()

    def _settle(self) -> None:
        """Once the search is done and every chunk is back: score what the processes couldn't, then finish."""
        if self._doing is None or self._slices.running() or self._out:
            return
        if self._retry:
            self._slices.start(self._score_here(self._doing[0], self._retry), self._settle)
            self._retry = []
        else:
            self._finished()

    def _apply(self, sub: str, items: list, results: list) -> None:
        """Keep and save the scores ``results`` of ``items`` [(kind, row, text)], and tell what they changed."""
        using, changed, stale = self._tally.setdefault(sub, (set(), {}, set()))
        saves = {k: [] for k in KINDS}
        for (kind, r, _), (s, label, usual) in zip(items, results):
            item = (kind, r["id"])
            using.add(item)
            if label != usual:
                changed[item] = (usual, label)
            else:
                changed.pop(item, None)
                if label != r["label"]:
                    stale.add(item)
            if (s, label) != (r["sentiment"], r["label"]):
                self._batch[kind].append(r._replace(sentiment=s, label=label))
                saves[kind].append((r["id"], s, label))
        for kind, scores in saves.items():
            if scores:
                self.store.rescore(kind, scores)
        self._counts[3] += len(items)
        self._tick(sub)

    def _tick(self, sub: str) -> None:
        """Commit now and then, and send out what's done every BATCH seconds, with how far it's got."""
        now = time.perf_counter()
        if now - self._sent_at > self.BATCH:
            self._save(sub)
            total, searched, found, scored = self._counts
            likely = found * total / max(searched, 1)  # found by the end, at this rate
            done = (searched + self.SCORE_COST * scored) / max(total + self.SCORE_COST * likely, 1)
            self._shown = max(self._shown, round(1000 * done))  # an estimate that grew doesn't take it back
            self.progress.emit(sub, self._shown, 1000)
            self._sent_at = self._committed_at = time.perf_counter()
        elif now - self._committed_at > self.COMMIT:
            self.store.commit()
            self._committed_at = now

    def _save(self, sub: str) -> None:
        self.store.commit()
        for kind, rows in self._batch.items():
            if rows:
                self.rescored.emit(sub, kind, rows)
        self._batch = {k: [] for k in KINDS}

    def _finished(self) -> None:
        sub, words = self._doing
        self._save(sub)
        self._doing = None
        if not self._todo:
            self.pool.rest()
        using, changed, stale = self._tally.pop(sub, (set(), {}, set()))
        self.scored.emit(sub, words, len(using), Counter(changed.values()), len(stale - changed.keys()))
        self.finished.emit(sub)
        self._next()


class MeaningSlider(QSlider):
    """From negative (left, in the timeline's orange) through neutral to positive (right, blue), as -100..100.
    The track shades between the two, so where the handle sits reads at a glance."""

    def __init__(self):
        super().__init__(Qt.Orientation.Horizontal)
        self.setRange(-100, 100)
        self.setSingleStep(5)
        self.setPageStep(25)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def sizeHint(self) -> QSize:
        return QSize(160, 22)

    def minimumSizeHint(self) -> QSize:
        return QSize(80, 22)

    def _x(self, value: int) -> float:
        r = 9  # the handle's radius: it stays inside the widget at either end
        return r + (self.width() - 2 * r) * (value - self.minimum()) / (self.maximum() - self.minimum())

    def mousePressEvent(self, e) -> None:  # jump to where it's pressed, then drag from there
        if e.button() == Qt.MouseButton.LeftButton:
            span = max(self.width() - 18, 1)
            self.setSliderDown(True)
            self.setValue(round(self.minimum() + (e.position().x() - 9) / span * (self.maximum() - self.minimum())))
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self.isSliderDown():
            span = max(self.width() - 18, 1)
            self.setValue(round(self.minimum() + (e.position().x() - 9) / span * (self.maximum() - self.minimum())))

    def mouseReleaseEvent(self, e) -> None:
        if self.isSliderDown():
            self.setSliderDown(False)  # sends sliderReleased

    def wheelEvent(self, e) -> None:
        if self.hasFocus():
            super().wheelEvent(e)
        else:  # scrolling the list over it scrolls the list
            e.ignore()

    def paintEvent(self, event) -> None:
        colors, t = palette(), theme.tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid, h = self.height() / 2, 6.0
        left, right = self._x(self.minimum()), self._x(self.maximum())
        grad = QLinearGradient(left, 0, right, 0)
        faint = QColor(colors["muted"])
        faint.setAlphaF(0.35)
        grad.setColorAt(0, QColor(colors["negative"]))
        grad.setColorAt(0.5, faint)
        grad.setColorAt(1, QColor(colors["positive"]))
        p.setPen(Qt.PenStyle.NoPen)
        p.setOpacity(1.0 if self.isEnabled() else 0.4)
        p.setBrush(grad)
        p.drawRoundedRect(QRectF(left, mid - h / 2, right - left, h), h / 2, h / 2)
        centre = self._x(0)
        p.setPen(QPen(QColor(colors["text"]), 1.5))
        p.drawLine(QPointF(centre, mid - 6), QPointF(centre, mid + 6))  # neutral
        v = self.value()
        knob = QColor(colors["positive"] if v > 0 else colors["negative"] if v < 0 else colors["muted"])
        p.setPen(QPen(QColor(t["accent"]) if self.hasFocus() else QColor(colors["surface"]), 2))
        p.setBrush(knob)
        p.drawEllipse(QPointF(self._x(v), mid), 7.5, 7.5)


class _Meaning(QWidget):
    """One word's row in WordMeanings: the word, how it reads and a remove button over its slider, then one line
    on what setting it did, with a hairline under it all."""

    def __init__(self, word: str, weight: float | None):
        super().__init__()
        self.word = word
        lay = QVBoxLayout(self)
        lay.setContentsMargins(2, 6, 0, 0)
        lay.setSpacing(2)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = ElidedLabel(word)
        self.name.setStyleSheet("font-weight: 600;")
        self.reads = label("", "caption")
        self.remove = flat_button("\u2715")
        self.remove.setToolTip("Let it mean what it usually does again")
        top.addWidget(self.name, 1)
        top.addWidget(self.reads)
        top.addWidget(self.remove)
        lay.addLayout(top)
        self.slider = MeaningSlider()
        self.slider.setToolTip(f"What \u201c{word}\u201d means here, from negative to positive")
        lay.addWidget(self.slider)
        self.result = ElidedLabel("", "caption")  # what setting it did to the saved items (WordMeanings.show_result)
        self.result.hide()
        lay.addWidget(self.result)
        lay.addSpacing(6)
        lay.addWidget(theme.mark(QFrame(), "role", "divider"))
        self.scoring = False  # its saved items are being scored again
        self.slider.valueChanged.connect(self._show)
        self.set(weight)

    def show_result(self, text: str, details: str = "") -> None:
        self.result.setText(text)
        self.result.setToolTip(details)
        self.result.show()

    def set(self, weight: float | None) -> None:
        """Show ``weight``, or None for a word picked but not set yet."""
        self.saved = weight is not None
        self.slider.blockSignals(True)
        self.slider.setValue(round((weight or 0) * 100))
        self.slider.blockSignals(False)
        self._show()

    def weight(self) -> float:
        v = self.slider.value()
        return 0.0 if abs(v) <= 7 else v / 100  # near the middle snaps to neutral

    def _show(self) -> None:
        moved = self.saved or self.slider.isSliderDown() or self.slider.value()
        self.reads.setText(meaning(self.weight()) if moved else "Not set")


class WordMeanings(QWidget):
    """What words mean in the subreddit shown: each set one with its slider, newest at the top. A word picked
    in the cloud, or typed in, comes up unset (it still means what it usually does) until its slider is
    moved. Sends ``changed`` with (word, weight) once a slider is let go, or (word, None) when one is removed."""

    changed = Signal(str, object)

    WIDTH = 300

    def __init__(self):
        super().__init__()
        self.setFixedWidth(self.WIDTH)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.entry = QLineEdit()
        self.entry.setClearButtonEnabled(True)
        self.entry.returnPressed.connect(self._add_typed)
        lay.addWidget(self.entry)
        self.status = label("", "caption")  # only when scoring failed
        self.status.setWordWrap(True)
        self.status.hide()
        lay.addWidget(self.status)
        self.scroll = VerticalScrollArea()
        inner = QWidget()
        self.list = QVBoxLayout(inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(0)
        self.empty = label("No words set yet, so they mean what they usually do.", "caption")
        self.empty.setWordWrap(True)
        self.list.addWidget(self.empty)
        self.list.addStretch(1)
        self.scroll.setWidget(inner)
        lay.addWidget(self.scroll, 1)
        self.rows: dict[str, _Meaning] = {}
        self.subreddit: str | None = None
        # A slider moved with the keys is saved once it rests, not at each step.
        self._rest = QTimer(self)
        self._rest.setSingleShot(True)
        self._rest.setInterval(500)
        self._rest.timeout.connect(self._save_resting)
        self._resting: _Meaning | None = None
        self.set_subreddit(None, {})

    def set_subreddit(self, name: str | None, words: dict[str, float]) -> None:
        """Show the words set for ``name`` (None: nothing shown)."""
        if self._rest.isActive():  # a slider moved with the keys a moment ago
            self._rest.stop()
            self._save_resting()
        self.subreddit = name
        for row in self.rows.values():
            row.deleteLater()
        self.rows = {}
        for word, weight in sorted(words.items(), reverse=True):
            self._row(word, weight)
        self.entry.setPlaceholderText("Click a word in the cloud, or type one" if name
                                      else "Open a subreddit to set what its words mean")
        self.entry.setEnabled(bool(name))
        self.status.hide()
        self._update_empty()

    def pick(self, word: str) -> None:
        """Bring ``word`` to the top of the list, unset if it isn't set yet, with its slider in focus."""
        word = Lexicon.key(word)
        if not word or not self.subreddit:
            return
        row = self.rows.get(word)
        if row is None:
            motion.crossfade(self.scroll.viewport())
            row = self._row(word, None)
        else:
            self.list.removeWidget(row)
            self.list.insertWidget(0, row)
        self._update_empty()
        self.scroll.verticalScrollBar().setValue(0)
        row.slider.setFocus()

    def show_failed(self, subreddit: str, message: str) -> None:
        if self.subreddit and subreddit == self.subreddit.lower():
            self.status.setText(f"Couldn't score saved items again: {message}")
            self.status.show()
            for row in self.rows.values():
                if row.scoring:
                    row.scoring = False
                    row.result.hide()

    def show_result(self, subreddit: str, words, using: int, changed: Counter, stale: int = 0) -> None:
        """Under each of ``words``, what scoring the saved items again by them did: so it shows the setting is
        at work, even where the cloud's colours barely move (a word is coloured by the items using it). Only
        the labels the setting itself changes count; ``stale`` items whose label was only out of date are
        mentioned apart."""
        if not (self.subreddit and subreddit == self.subreddit.lower()):
            return
        if not using:
            text, details = "No saved items use it yet", ""
        else:
            moved = sum(changed.values())
            text = f"\u2713 {f'{moved:,}' if moved else 'None'} of {using:,} saved items changed label"
            details = "\n".join(f"{n:,} {a} \u2192 {b}" for (a, b), n in changed.most_common())
            if stale:  # out of date, not changed by the setting
                details += f"{chr(10) if details else ''}Also updated {stale:,} scored by an older version"
        for word in words:
            row = self.rows.get(word)
            if row is not None:
                row.scoring = False
                row.show_result(text, details)

    def show_progress(self, subreddit: str, done: int | None = None, total: int = 0) -> None:
        """How far scoring the saved items again has got, under the words being scored; done None once it's
        finished."""
        if done is not None and self.subreddit and subreddit == self.subreddit.lower():
            for row in self.rows.values():
                if row.scoring:
                    row.show_result(f"Scoring saved items\u2026 {100 * done // max(total, 1)}%")

    def _row(self, word: str, weight: float | None) -> _Meaning:
        row = self.rows[word] = _Meaning(word, weight)
        self.list.insertWidget(0, row)
        row.slider.sliderReleased.connect(lambda: self._save(row))
        row.slider.valueChanged.connect(lambda: self._moved(row))
        row.remove.clicked.connect(lambda: self._remove(row))
        return row

    def _moved(self, row: _Meaning) -> None:
        if not row.slider.isSliderDown():  # the keys or the wheel
            self._resting = row
            self._rest.start()

    def _save_resting(self) -> None:
        if self._resting is not None and self.rows.get(self._resting.word) is self._resting:
            self._save(self._resting)
        self._resting = None

    def _save(self, row: _Meaning) -> None:
        weight = row.weight()
        row.set(weight)  # snapped, and set from now on
        row.scoring = True
        row.show_result("Scoring saved items\u2026")
        self.changed.emit(row.word, weight)

    def _remove(self, row: _Meaning) -> None:
        motion.crossfade(self.scroll.viewport())
        self.rows.pop(row.word, None)
        row.deleteLater()
        self._update_empty()
        if row.saved:
            self.changed.emit(row.word, None)

    def _add_typed(self) -> None:
        word = Lexicon.key(self.entry.text())
        if word:
            self.entry.clear()
            self.pick(word)

    def _update_empty(self) -> None:
        self.empty.setVisible(not self.rows)
