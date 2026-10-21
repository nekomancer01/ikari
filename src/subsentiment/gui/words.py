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

from PySide6.QtCore import QEvent, QObject, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetricsF, QLinearGradient, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLineEdit, QPushButton, QSizePolicy, QSlider, QToolTip, QVBoxLayout, QWidget,
)

from subsentiment.analysis import WordCounts
from subsentiment.gui import motion, theme
from subsentiment.gui.charts import palette
from subsentiment.gui.days import flat_button
from subsentiment.gui.widgets import ElidedLabel, VerticalScrollArea, label
from subsentiment.gui.worker import BackgroundJob, LoadWorker, Slices
from subsentiment.sentiment import Lexicon

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
        self._seen = {k: {} for k in KINDS}  # {id: (text, words)}: each row's text is split into words once
        self.counts = {k: WordCounts(k, (), self._seen[k]) for k in KINDS}
        self.ready = True
        self._pending: dict[str, list] = {k: [] for k in KINDS}  # rows added while a recount runs
        self._slices = Slices(self)
        self._version = 0  # counts every change to the counts, for top()
        self._top, self._top_for = [], None

    def count(self, posts: list, comments: list, fresh: bool = False) -> None:
        """Count these rows' words instead. Rows the counts have already are kept, so moving the span a few days
        only counts the days it gained and takes off the ones it left; ``fresh`` (rows that may have been saved
        again with a new label or text) counts them all again, still without splitting a known text again."""
        self._slices.stop()
        self._pending = {k: [] for k in KINDS}  # rows added for an earlier count's rows don't carry over
        rows = {"posts": list(posts), "comments": list(comments)}
        if sum(map(len, self._seen.values())) > self.SEEN_MAX:
            self._seen = {k: {} for k in KINDS}
            for k in KINDS:
                self.counts[k].seen = self._seen[k]
        if self.ready and not fresh:
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

    word_clicked = Signal(str)

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
                QToolTip.showText(e.globalPos(), f"{word}: in {n:,} {'post or comment' if n == 1 else 'posts and comments'}"
                                                 f", {way}. Click to set what it means here", self)
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
    else read on a thread (the window's LoadWorker, which goes through the day packs), and scored a slice at a
    time between frames, each saved as it's scored. Every quarter second what's done is committed and sent out
    with ``rescored``, so the pages follow as it goes. A change while one runs starts it again with every word
    it was doing."""

    rescored = Signal(str, str, object)  # subreddit (lowercased), "posts" or "comments", the rows with new scores
    progress = Signal(str, int, int)  # subreddit, items gone through, of
    finished = Signal(str)  # subreddit: every item using the words is scored again and saved
    failed = Signal(str, str)  # subreddit, what went wrong reading its items

    BATCH = 0.25  # seconds between two batches sent out
    COMMIT = 0.05  # seconds between two commits: other connections (a fetch) wait while one is open

    def __init__(self, store, db_path: str, parent: QObject | None = None):
        super().__init__(parent)
        self.store, self.db_path = store, db_path
        self.job = BackgroundJob(self)
        self.job.ended.connect(self._next)
        self._slices = Slices(self)
        self._todo: dict[str, set[str]] = {}  # subreddit -> words to rescore for, waiting their turn
        self._doing: tuple[str, set[str]] | None = None
        self._rows: dict[str, dict | None] = {}  # what start was given to go through, by subreddit
        self._run = 0  # counts runs, so a read for one dropped since is ignored
        self._batch: dict[str, list] = {k: [] for k in KINDS}  # rescored, not saved yet

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
        if self.job.busy:
            self.job.worker.finished.disconnect()
            self.job.worker.failed.disconnect()
            self.job.qthread.quit()
            self.job.qthread.wait(3000)

    def _drop(self) -> None:
        self._slices.stop()
        if self._doing is not None:
            self._save(self._doing[0])
        self._doing = None
        self._run += 1

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
        worker = LoadWorker(self.db_path, sub, None, self._run)
        worker.finished.connect(self._on_read)
        worker.failed.connect(lambda message, sub=sub: (self._drop(), self.failed.emit(sub, message)))
        self.job.start(worker)

    def _on_read(self, read: tuple) -> None:
        run, rows, _ = read
        if run == self._run and self._doing is not None:
            self._slices.start(self._steps(*self._doing, rows), self._finished)

    def _steps(self, sub: str, words: set[str], rows: dict):
        """Score the items using ``words`` again, yielding after each; saved a batch at a time."""
        lexicon, uses = self.store.lexicon(sub), Lexicon(dict.fromkeys(words, 0.0)).uses
        total, done = sum(map(len, rows.values())), 0
        sent_at = committed_at = time.perf_counter()
        for kind in KINDS:
            for r in rows[kind]:
                text = (r["body"] or "") if kind == "comments" else f"{r['title'] or ''}\n\n{r['selftext'] or ''}".strip()
                done += 1
                if uses(text):
                    s, label = lexicon.score(text)
                    if (s, label) != (r["sentiment"], r["label"]):
                        self._batch[kind].append({**r, "sentiment": s, "label": label})
                        self.store.rescore(kind, [(r["id"], s, label)])
                now = time.perf_counter()
                if now - sent_at > self.BATCH:
                    self._save(sub)
                    self.progress.emit(sub, done, total)
                    sent_at = committed_at = time.perf_counter()
                elif now - committed_at > self.COMMIT:
                    self.store.commit()
                    committed_at = now
                yield

    def _save(self, sub: str) -> None:
        self.store.commit()
        for kind, rows in self._batch.items():
            if rows:
                self.rescored.emit(sub, kind, rows)
        self._batch = {k: [] for k in KINDS}

    def _finished(self) -> None:
        sub = self._doing[0]
        self._save(sub)
        self._doing = None
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


class _Meaning(QFrame):
    """One word's row in WordMeanings: the word, how it reads, a remove button and the slider."""

    def __init__(self, word: str, weight: float | None):
        super().__init__()
        self.word = word
        self.setProperty("glass", "solid")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 6, 8)
        lay.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(6)
        self.name = ElidedLabel(word)
        self.name.setStyleSheet("font-weight: 600;")
        self.reads = label("", "caption")
        self.remove = flat_button("✕")
        self.remove.setToolTip("Let it mean what it usually does again")
        top.addWidget(self.name, 1)
        top.addWidget(self.reads)
        top.addWidget(self.remove)
        lay.addLayout(top)
        self.slider = MeaningSlider()
        self.slider.setToolTip(f"What \u201c{word}\u201d means here, from negative to positive")
        lay.addWidget(self.slider)
        self.slider.valueChanged.connect(self._show)
        self.set(weight)

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
        self.intro = label("", "caption")
        self.intro.setWordWrap(True)
        lay.addWidget(self.intro)
        add = QHBoxLayout()
        add.setSpacing(6)
        self.entry = QLineEdit()
        self.entry.setPlaceholderText("Word or phrase")
        self.entry.returnPressed.connect(self._add_typed)
        self.add_btn = QPushButton("Add")
        self.add_btn.setAutoDefault(False)
        self.add_btn.clicked.connect(self._add_typed)
        self.entry.textChanged.connect(lambda text: self.add_btn.setEnabled(bool(Lexicon.key(text))))
        self.add_btn.setEnabled(False)
        add.addWidget(self.entry, 1)
        add.addWidget(self.add_btn)
        lay.addLayout(add)
        self.status = ElidedLabel("", "caption")
        lay.addWidget(self.status)
        self.scroll = VerticalScrollArea()
        inner = QWidget()
        self.list = QVBoxLayout(inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(6)
        self.empty = label("No words set yet. Usual meanings apply.", "caption")
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
        self.intro.setText(f"What a word means in r/{name}, from negative to positive. Click one in the cloud, "
                           "or type it." if name else "Open a subreddit to set what its words mean.")
        for w in (self.entry, self.add_btn):
            w.setEnabled(bool(name) and (w is self.entry or bool(Lexicon.key(self.entry.text()))))
        self.status.setText("")
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

    def show_progress(self, subreddit: str, done: int | None = None, total: int = 0) -> None:
        """How far scoring the saved items again has got; done None once it's finished."""
        if self.subreddit and subreddit == self.subreddit.lower():
            self.status.setText("Saved items scored again" if done is None
                                else f"Scoring saved items again… {100 * done // max(total, 1)}%")

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
