"""The word cloud: the words the shown posts and comments use most, sized by how many use them and coloured
by which way those lean (the timeline's blue for positive, orange for negative).

``WordTally`` has the words of the rows on screen, which their RowIndexes count from each day's word counts on
a thread: moving the span or a score filter, or a fetch adding rows, counts them again in a few hundredths of a
second, the window going on meanwhile.

Clicking a word picks it in ``WordMeanings``, beside the cloud on the Words page, where a slider sets what it
means in the subreddit shown, from negative through neutral to positive (sentiment.Lexicon). ``Rescore`` then
scores the saved posts and comments using it again, a slice at a time, and every page follows as it goes.
"""
from __future__ import annotations

import math
import queue
import sqlite3
import threading
import time
from collections import Counter

from PySide6.QtCore import (QEasingCurve, QEvent, QObject, QPointF, QRectF, QSize, QSizeF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLineEdit, QSizePolicy, QSlider, QToolTip, QVBoxLayout, QWidget,
)

from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
from subsentiment.gui.days import flat_button
from subsentiment.gui.widgets import ElidedLabel, VerticalScrollArea, label
from subsentiment.analysis import LABELS, TEXT
from subsentiment.gui.rows import RowSource, Saved
from subsentiment.gui.worker import BackgroundJob, LoadWorker, ScorePool, Slices, on_thread
from subsentiment.index import top_words
from subsentiment.sentiment import Lexicon
from subsentiment.store import Store

KINDS = ("posts", "comments")
_CELL = 2  # px; the word cloud's grid of taken space
_FONTS: dict[int, object] = {}  # the cloud's font by pixel size


class WordTally(QObject):
    """The words the posts and comments on screen use most, as their RowIndexes count them (index.top_words: each
    day's counts added up: a few hundredths of a second for 650,000 comments, on a thread, where numpy lets go of the
    interpreter lock). ``count`` asks for the words of a span and score filters; ``ready`` is False until the words
    of a span or filter asked for the first time are in, and ``changed`` fires each time new ones are."""

    changed = Signal()
    N = 150  # words worked out: the most a cloud shows
    THREAD_FROM = 20_000  # posts and comments from which they're counted on a thread; fewer, at once
    SCOPES = (KINDS, ("posts",), ("comments",))

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.ready = True
        self._top: dict[tuple, list] = {}  # kinds -> their words, as last counted
        self._want: tuple | None = None  # (saved, days, min_scores, their versions) to count next
        self._shown: tuple | None = None  # what the words in _top are of, the same way
        self._slices = Slices(self)

    def count(self, saved: dict, days: tuple[int, int] | None, min_scores: tuple) -> None:
        """Count the words of ``saved``'s rows ({kind: rows.RowSource}) created in ``days`` that score at least
        ``min_scores`` (posts', comments'), unless they're counted as they are now. The words shown meanwhile stay
        up; another span's or filter's are only taken once they're in (``ready``)."""
        want = (saved, days, tuple(min_scores), tuple(saved[k].version for k in KINDS))
        if self._same(want, self._want if self._slices.running() else self._shown, versions=True):
            return
        if not self._same(want, self._shown):
            self.ready = False
        self._want = want
        if sum(len(saved[k]) for k in KINDS) < self.THREAD_FROM:  # (a big count still running is let go)
            self._slices.stop()
            self._take(want, self._counted(want))
        elif not self._slices.running():
            self._slices.start(self._counting(), lambda: None)

    @staticmethod
    def _same(a: tuple | None, b: tuple | None, versions: bool = False) -> bool:
        """Whether ``a`` and ``b`` are of the same rows, span and filters (and, with ``versions``, as they are)."""
        return (a is not None and b is not None and a[0] is b[0] and a[1:3] == b[1:3]
                and (not versions or a[3] == b[3]))

    def busy(self) -> bool:
        """Words asked for are being counted."""
        return not self.ready or self._slices.running()

    def hold(self) -> None:
        """Not ready until the next count: the rows to count are on their way."""
        self._slices.stop()
        self._want, self._shown, self.ready = None, None, False
        self.changed.emit()

    def top(self, kinds, n: int = 100) -> list[tuple[str, int, float]]:
        """The ``n`` words used most among ``kinds``, as last counted."""
        return self._top.get(tuple(kinds), [])[:n]

    def _counted(self, want: tuple) -> dict:
        saved, days, (min_post, min_comment), _ = want
        parts = {k: (saved[k].index, least) for k, least in zip(KINDS, (min_post, min_comment))
                 if isinstance(saved.get(k), Saved)}
        return {kinds: top_words([parts[k] for k in kinds if k in parts], days, self.N) if parts else []
                for kinds in self.SCOPES}

    def _counting(self):
        """Count what's wanted on a thread; once it's in, what was asked for meanwhile, if anything."""
        while self._want is not None and not self._same(self._want, self._shown, versions=True):
            want = self._want
            got = yield from on_thread(lambda want=want: self._counted(want))
            self._take(want, got)

    def _take(self, want: tuple, got: dict) -> None:
        self._top, self._shown = got, want
        if self._same(self._want, want):
            self.ready = True
        self.changed.emit()


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


class WordCloud(theme.OnCard):
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
    RESIZE_HOLD_MS = 120  # a size still changing (a window edge dragged) is let settle this long before placing

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
        # While the size keeps changing the words stay as they were placed, centred (_offset), and are placed for
        # the size it ends at: placing them again at every step of a resize took longer than the frame.
        self._resizing = QTimer(self, singleShot=True, interval=self.RESIZE_HOLD_MS)
        self._resizing.timeout.connect(self.update)

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
        self._resizing.stop()
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
        held = self._resizing.isActive() and before is not None and before[2] == key[2]  # (new words aren't)
        if self._placing is None and before != key and not held:
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
        dx, dy = self._offset(key)  # where the words were drawn while they waited, so they ease on from there
        for w in self._shown.values():
            w.x, w.y = w.x + dx, w.y + dy
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

    def _offset(self, size: tuple | None = None) -> tuple[int, int]:
        """How far the words placed for another size (the size before a resize) are moved to stay centred in
        ``size`` (by default the cloud's own)."""
        w, h = size[:2] if size else (self.width(), self.height())
        placed = self._placed_for
        return (0, 0) if placed is None else ((w - placed[0]) // 2, (h - placed[1]) // 2)

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
        w, h = self._placing_for[0][:2]  # the size placed for, though it may change while they're placed
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
        steps = self._draw(self._next, colors, dpr, QSize(w, h))
        picture = next(steps)
        yield from steps
        self._next_drawn = (picture, (self._placing_for[0], dpr, tuple(colors.values())))

    def _draw(self, placed: list, colors: dict, dpr: float, size: QSize):
        """Draw the words at rest, placed for ``size``, into a picture: yields the picture, then once after each
        word."""
        picture = QPixmap(size * dpr)
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
        p = self.painter()
        if self._placing is not None and not self._placing_for[1]:
            return  # the words that were here before it was shown aren't shown again; the new ones pop in shortly
        if not self._shown:
            if self._placing is None:
                p.setPen(QColor(colors["muted"]))
                p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Counting words…" if self.busy else self.empty)
            self._painted = True
            return
        dpr = self.devicePixelRatioF()
        p.translate(*self._offset())
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
            steps = self._draw(self._placed, colors, dpr, QSize(*self._placed_for[:2]))
            self._drawn, self._drawn_for = next(steps), key
            for _ in steps:
                pass
        p.setOpacity(self._dim)
        p.drawPixmap(0, 0, self._drawn)
        self._painted = True

    def resizeEvent(self, event) -> None:
        if self._placed_for is not None and self._painted and self.isVisible():
            self._resizing.start()  # placed again once the size holds still
        super().resizeEvent(event)

    def hideEvent(self, event) -> None:
        self._painted = False  # shown again, it starts from its new words rather than easing from old ones
        super().hideEvent(event)

    def word_at(self, pos: QPointF) -> str | None:
        pos = pos - QPointF(*self._offset())
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


class ScoreWriter(QObject):
    """Saves Rescore's new scores on a thread of its own, on a connection of its own: saving a quarter second's
    and committing them (with SQLite's checkpoint now and then) held the window up for as much as half a second.
    ``saved`` brings each lot's tag back once it's committed, in the order they were given, with what went wrong
    saving it ("" when nothing did): a lot that can't be saved (another connection holding the database past
    SQLite's timeout, each try) is tried TRIES times."""

    saved = Signal(object, str)
    TRIES = 3

    def __init__(self, db_path: str, parent: QObject | None = None):
        super().__init__(parent)
        self.db_path = db_path
        self._lots: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def save(self, scores: dict, tag) -> None:
        """Save ``scores`` ({kind: [(id, sentiment, label)]}) after what's given before, then send ``tag``."""
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="ScoreWriter", daemon=True)
            self._thread.start()
        self._lots.put((scores, tag))

    def stop(self) -> None:
        """Save what's been given, and end the thread (the window is closing)."""
        if self._thread is not None:
            self._stopping.set()  # (tried once more each, not three times)
            self._lots.put(None)
            self._thread.join(10)
            self._thread = None
            self._stopping.clear()

    def _run(self) -> None:
        store = None
        while (lot := self._lots.get()) is not None:
            scores, tag = lot
            for tried in range(1, self.TRIES + 1):
                try:
                    store = store or Store(self.db_path)
                    for kind, rows in scores.items():
                        if rows:
                            store.rescore(kind, rows)
                    store.commit()
                    error = ""
                    break
                except Exception as e:  # noqa: BLE001 - locked past its timeout, say
                    error = f"their new scores couldn't be saved ({e})"
                    if store is not None:
                        try:
                            store.db.rollback()
                        except sqlite3.Error:
                            pass
                    if self._stopping.is_set() or tried == self.TRIES:
                        break
                    time.sleep(1.0)
            try:
                self.saved.emit(tag, error)
            except RuntimeError:  # the window has closed
                break
        if store is not None:
            store.close()


class Rescore(QObject):
    """Scores a subreddit's saved posts and comments again by its words as they're set now, once some words'
    meanings there changed: only the items using one of those words. They're the rows the window has of it (whose
    text is read on a thread), or else read on a thread (the window's LoadWorker). They're searched for the
    words a slice at a time between frames, and what's found is scored by a ScorePool in other processes, a
    chunk at a time, or here between frames when there's little of it or the processes can't run, along with the
    words each uses. Every quarter second what's been scored is saved by a ScoreWriter on its thread, then sent
    out with ``rescored``, so the pages follow as it goes. A change while one runs starts it again with every word
    it was doing."""

    # subreddit (lowercased), "posts" or "comments", [{id, sentiment, label, words}]: saved by then
    rescored = Signal(str, str, object)
    progress = Signal(str, int, int)  # subreddit, thousandths done, 1000
    finished = Signal(str)  # subreddit: every item using the words is scored again and saved
    # Just before finished: subreddit, the words, how many items use them, how many labels the words changed
    # each way ({(label without them, label with them): items}), and how many others' scores were only out of
    # date (scored by an older version).
    scored = Signal(str, object, int, object, int)
    failed = Signal(str, str)  # subreddit, what went wrong reading its items or saving their scores

    BATCH = 0.25  # seconds between two batches saved and sent out
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
        self.writer = ScoreWriter(db_path, self)
        self.writer.saved.connect(self._written)
        self._writing: Counter = Counter()  # batches with the writer, by subreddit
        self._unsaved: dict[str, str] = {}  # subreddit -> what went wrong saving some of its run's scores
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
        self._sent_at = 0.0

    def start(self, subreddit: str, words, rows: dict | None = None) -> None:
        """Rescore for ``words``; ``rows`` ({kind: rows, or a rows.RowSource}) are every saved post and comment of
        it, when the window has them all, so they needn't be read again."""
        sub = subreddit.lower()
        words = {Lexicon.key(w) for w in words}
        if self._doing is not None and self._doing[0] == sub:
            words |= self._doing[1]
            self._drop()
        self._todo[sub] = self._todo.get(sub, set()) | words
        self._rows[sub] = None if rows is None else {  # as they are now
            k: v if isinstance(v, RowSource) else list(v) for k, v in rows.items()}
        self._next()

    def running(self, subreddit: str | None = None) -> bool:
        """Whether a subreddit (any) is being rescored, or what's been is still being saved."""
        doing = [self._doing[0]] if self._doing else []
        return any(subreddit is None or s == subreddit.lower() for s in [*doing, *self._todo, *self._writing])

    def stop(self) -> None:
        """Save what's been rescored and stop (the window is closing); its read's and writer's threads are
        waited for."""
        self._todo.clear()
        self._rows.clear()
        self._drop()
        self.pool.close()
        self.writer.stop()
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
        if self._doing is not None or self.job.busy or not self._todo or self._writing:
            return  # (the next run reads the scores saved: once they all are)
        sub = next(iter(self._todo))
        self._doing = (sub, self._todo.pop(sub))
        self._run += 1
        rows = self._rows.pop(sub, None)
        if rows is not None:
            self._on_read((self._run, rows))
            return
        worker = LoadWorker(self.db_path, sub, self._run, rows=True)  # every row, whatever the window filters
        worker.read.connect(self._on_read)
        worker.failed.connect(lambda message, sub=sub: (self._drop(), self._tally.pop(sub, None),
                                                        self.failed.emit(sub, message)))
        self.job.start(worker)

    def _on_read(self, read: tuple) -> None:
        run, rows = read
        if run == self._run and self._doing is not None:
            self._slices.start(self._search(*self._doing, rows), self._settle)

    def _search(self, sub: str, words: set[str], rows: dict):
        """Find the items using ``words``, yielding after each, and send them to be scored a chunk at a time."""
        # As saved now: the store's lexicon can be a second behind one an import saved on its own connection.
        self._lexicon = (Lexicon(self.store.words(sub)).weights, frozenset(words))
        uses = Lexicon(dict.fromkeys(words, 0.0)).uses
        for kind in KINDS:  # a source's text is read first: on a thread, a big step at a time
            if isinstance(rows[kind], RowSource):
                for key in TEXT[kind]:
                    yield from rows[kind].read_values(key)
        self._counts, self._shown = [sum(map(len, rows.values())), 0, 0, 0], 0
        self._sent_at = time.perf_counter()
        chunk, sent = [], False
        for kind in KINDS:
            for r, text in self._texts(kind, rows[kind]):
                self._counts[1] += 1
                if uses(text):
                    self._counts[2] += 1
                    chunk.append((kind, r(), text))
                    if len(chunk) == self.CHUNK:
                        self._submit(sub, chunk)
                        chunk, sent = [], True
                self._tick(sub)
                yield
        if sent and chunk:
            self._submit(sub, chunk)
        elif chunk:
            yield from self._score_here(sub, [chunk])

    @staticmethod
    def _texts(kind: str, rows):
        """(a call giving the row's (id, sentiment, label), its text) for each of ``rows`` (a list, or a
        rows.RowSource). As a tuple, a row found waiting to be scored is let alone by the garbage collector: as dicts,
        those waiting doubled how long each of its passes held up the window."""
        def text(*parts):
            return (parts[0] or "") if kind == "comments" else f"{parts[0] or ''}\n\n{parts[1] or ''}".strip()
        if not isinstance(rows, RowSource):
            for r in rows:
                yield (lambda r=r: (r["id"], r["sentiment"], r["label"])), text(*(r[k] for k in TEXT[kind]))
            return
        ids, sentiment, label = rows.ids, rows.sentiment, rows.label
        for i, parts in enumerate(zip(*(rows.values(k) for k in TEXT[kind]))):
            yield (lambda i=i: (ids[i].decode() if isinstance(ids[i], bytes) else ids[i], float(sentiment[i]),
                                LABELS[label[i]] if label[i] >= 0 else None)), text(*parts)

    def _submit(self, sub: str, chunk: list) -> None:
        self._out += 1
        self.pool.submit((self._run, sub, chunk), *self._lexicon, [t for *_, t in chunk])

    def _score_here(self, sub: str, chunks: list[list]):
        for chunk in chunks:
            for item in chunk:
                self._apply(sub, [item], Lexicon.compare(*self._lexicon, [item[2]], split=True))
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
        """Keep the scores ``results`` of ``items`` [(kind, (id, sentiment, label), text)] to save, and tell what
        they changed."""
        using, changed, stale = self._tally.setdefault(sub, (set(), {}, set()))
        for (kind, (rid, was, was_label), _), (s, label, usual, words) in zip(items, results):
            item = (kind, rid)
            using.add(item)
            if label != usual:
                changed[item] = (usual, label)
            else:
                changed.pop(item, None)
                if label != was_label:
                    stale.add(item)
            if (s, label) != (was, was_label):
                self._batch[kind].append({"id": rid, "sentiment": s, "label": label, "words": words})
        self._counts[3] += len(items)
        self._tick(sub)

    def _tick(self, sub: str) -> None:
        """Save what's done every BATCH seconds, and tell how far it's got."""
        if time.perf_counter() - self._sent_at > self.BATCH:
            self._save(sub)
            total, searched, found, scored = self._counts
            likely = found * total / max(searched, 1)  # found by the end, at this rate
            done = (searched + self.SCORE_COST * scored) / max(total + self.SCORE_COST * likely, 1)
            self._shown = max(self._shown, round(1000 * done))  # an estimate that grew doesn't take it back
            self.progress.emit(sub, self._shown, 1000)
            self._sent_at = time.perf_counter()

    def _save(self, sub: str, then=None) -> None:
        """Hand what's been rescored to the writer; it's sent out once saved, and ``then`` called."""
        batch, self._batch = self._batch, {k: [] for k in KINDS}
        if any(batch.values()) or then is not None:
            self._writing[sub] += 1
            self.writer.save({k: [(r["id"], r["sentiment"], r["label"]) for r in rows] for k, rows in batch.items()},
                             (sub, batch, then))

    def _written(self, tag, error: str) -> None:
        sub, batch, then = tag
        self._writing[sub] -= 1
        if not self._writing[sub]:
            del self._writing[sub]
        if error:  # the pages keep the scores those items had; told once the run is done
            self._unsaved[sub] = error
        else:
            for kind, rows in batch.items():
                if rows:
                    self.rescored.emit(sub, kind, rows)
        if then is not None:
            error = self._unsaved.pop(sub, "")
            if error:  # what it did isn't what's saved: the pages catch up on what is, and it says what went wrong
                self.finished.emit(sub)
                self.failed.emit(sub, error)
            else:
                then()
        self._next()

    def _finished(self) -> None:
        sub, words = self._doing
        self._doing = None
        if not self._todo:
            self.pool.rest()
        using, changed, stale = self._tally.pop(sub, (set(), {}, set()))
        self._save(sub, lambda: (self.scored.emit(sub, words, len(using), Counter(changed.values()),
                                                  len(stale - changed.keys())), self.finished.emit(sub)))


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

    R = 9  # the handle's radius: it stays inside the widget at either end

    def _x(self, value: int) -> float:
        return self.R + (self.width() - 2 * self.R) * (value - self.minimum()) / (self.maximum() - self.minimum())

    def mousePressEvent(self, e) -> None:  # jump to where it's pressed, then drag from there
        if e.button() == Qt.MouseButton.LeftButton:
            self.setSliderDown(True)
            self.mouseMoveEvent(e)
            e.accept()
        else:
            super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:
        if self.isSliderDown():
            lo, hi, span = self.minimum(), self.maximum(), max(self.width() - 2 * self.R, 1)
            self.setValue(round(lo + (e.position().x() - self.R) / span * (hi - lo)))

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
        self.result = label("", "caption")  # what setting it did to the saved items (WordMeanings.show_result)
        self.result.setWordWrap(True)
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
        self.sent = weight  # as saved, and scored by (the slider may have moved on since)
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
        moved = sum(changed.values())
        details = "\n".join(f"{n:,} {a} \u2192 {b}" for (a, b), n in changed.most_common())
        if stale:  # out of date, not changed by the setting
            details += f"{chr(10) if details else ''}Also updated {stale:,} scored by an older version"
        items = f"{using:,} saved {'item uses' if using == 1 else 'items use'}"
        for word in words:
            row = self.rows.get(word)
            if row is None:
                continue
            it = "these words" if len(words) > 1 else f"\u201c{word}\u201d"
            if not using:
                text = f"No saved items use {it} yet"
            elif moved:
                text = f"\u2713 {moved:,} of {using:,} saved items changed label"
            elif all(not self.rows[w].sent and not Lexicon.known(w) for w in words if w in self.rows):
                # (The owner set "zenless" to Neutral, read "None of 61 changed" and took it for broken.)
                text = (f"\u2713 No labels changed. {items} {it}, but the scorer doesn't know "
                        f"{'them' if len(words) > 1 else 'the word'}, so it already read as neutral.")
            else:
                text = (f"\u2713 No labels changed. {items} {it}, but the setting wasn't enough to move any of "
                        "them to another label.")
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
