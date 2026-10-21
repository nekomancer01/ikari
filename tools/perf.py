"""How smooth the window feels with a big saved subreddit: frame times while the timeline is zoomed, dragged and
panned, and the stalls when a page or a saved subreddit is switched.

    QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 python tools/perf.py            # The owner's size: ~2,900 posts, ~48,000 comments
    QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=1.5 python tools/perf.py --scale 4  # four times that

Seeds throwaway databases (your own data and settings aren't touched), opens the real MainWindow at 1280x760
(1920x1200 at 150%, maximized) and drives it with mouse events at 60 per second while a 1 ms heartbeat timer
notes every gap in the event loop. A gap is how long the window couldn't paint or answer the mouse: the
longest one, and how many go past a 60 Hz frame (16.7 ms), are what an interaction feels like.
"""
from __future__ import annotations

import argparse
import collections
import cProfile
import itertools
import os
import pstats
import random
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QEventLoop, QPoint, QPointF, QSettings, Qt, QTimer
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication

from subsentiment.store import COMMENT_COLUMNS, POST_COLUMNS, Store

FRAME_MS = 1000 / 60
POSTS, COMMENTS, DAYS = 2853, 47955, 730
LABELS = ("positive", "neutral", "negative")


class Corpus:
    """Made-up words used as often as words in real text are (a few very often, most rarely)."""

    def __init__(self, rng: random.Random, size: int = 30000):
        sounds = ["ka", "ri", "to", "ne", "mu", "sa", "lo", "vi", "de", "po", "ga", "shi", "ren", "tal", "mor", "bel"]
        self.words = list({"".join(rng.choice(sounds) for _ in range(rng.randint(2, 4))) for _ in range(size)})
        self.cum = list(itertools.accumulate(1 / (i + 1) for i in range(len(self.words))))
        self.rng = rng

    def text(self, n: int) -> str:
        return " ".join(self.rng.choices(self.words, cum_weights=self.cum, k=n))


def seed(path: str, sub: str, scale: float, seed_no: int) -> None:
    """``scale`` times the owner's subreddit, over two years of days, written straight to the database."""
    Store(path).close()
    rng = random.Random(seed_no)
    corpus = Corpus(rng)
    now = time.time() // 86400 * 86400
    n_posts, n_comments = int(POSTS * scale), int(COMMENTS * scale)
    db = sqlite3.connect(path)
    created = [now - rng.betavariate(1, 2.5) * DAYS * 86400 for _ in range(n_posts)]
    posts = [(f"{sub}p{i}", sub, corpus.text(rng.randint(4, 14)), corpus.text(rng.randint(0, 40)), f"author{i % 400}",
              rng.randint(0, 9000), 0.9, rng.randint(0, 50), created[i], f"/r/{sub}/comments/p{i}/", "",
              rng.uniform(-1, 1), rng.choice(LABELS), now - 3600) for i in range(n_posts)]
    db.executemany(f"INSERT INTO posts ({','.join(POST_COLUMNS)}) VALUES ({','.join('?' * len(POST_COLUMNS))})", posts)
    comments = []
    for j in range(n_comments):
        i = rng.randrange(n_posts)
        comments.append((f"{sub}c{j}", f"{sub}p{i}", sub, f"t3_{sub}p{i}", f"user{j % 3000}",
                         corpus.text(rng.randint(3, 40)), rng.randint(-5, 900), created[i] + rng.uniform(0, 2 * 86400),
                         0, f"/r/{sub}/comments/c{j}/", rng.uniform(-1, 1), rng.choice(LABELS), now - 3600))
    db.executemany(f"INSERT INTO comments ({','.join(COMMENT_COLUMNS)}) VALUES ({','.join('?' * len(COMMENT_COLUMNS))})", comments)
    db.commit()
    db.close()


class Heartbeat:
    """Notes every gap in the event loop while it runs: a 1 ms timer that can only fire between other work."""

    def __init__(self):
        self.clock = QElapsedTimer()
        self.timer = QTimer()
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(1)
        self.timer.timeout.connect(self._beat)
        self.gaps: list[float] = []
        self.samples: list | None = None  # with --stalls: where the GUI thread was, sampled every 2 ms
        self.stalls: list = []

    def sample(self) -> None:
        """Note where the GUI thread is every 2 ms (from another thread), to say what each long gap was doing."""
        self.samples = []
        main = threading.main_thread().ident

        def run():
            while True:
                time.sleep(0.002)
                frame = sys._current_frames().get(main)
                trail = []
                while frame is not None and len(trail) < 3:
                    if "subsentiment" in frame.f_code.co_filename:
                        trail.append(f"{Path(frame.f_code.co_filename).stem}.{frame.f_code.co_name}:{frame.f_lineno}")
                    frame = frame.f_back
                self.samples.append(" < ".join(trail) or "Qt")
        threading.Thread(target=run, daemon=True).start()

    def _beat(self) -> None:
        now = self.clock.nsecsElapsed() / 1e6
        self.gaps.append(now - self.last)
        if self.samples is not None and now - self.last > 2 * FRAME_MS:
            self.stalls.append((now - self.last, collections.Counter(self.samples).most_common(4)))
        if self.samples is not None:
            self.samples.clear()
        self.last = now

    def run(self, steps: list, settle_ms: int = 1200) -> dict:
        """Run each step (a callable) a frame apart, then let timers and animations finish."""
        self.gaps = []
        loop = QEventLoop()
        queue = list(steps)
        driver = QTimer()
        driver.setTimerType(Qt.TimerType.PreciseTimer)
        driver.setInterval(round(FRAME_MS))

        def next_step():
            if queue:
                queue.pop(0)()
            else:
                driver.stop()
                QTimer.singleShot(settle_ms, loop.quit)
        driver.timeout.connect(next_step)
        self.clock.start()
        self.last = 0.0
        self.timer.start()
        driver.start()
        loop.exec()
        self.timer.stop()
        gaps = sorted(self.gaps)
        self.worst_ms = gaps[-1] if gaps else 0.0
        return {"worst": gaps[-1] if gaps else 0.0, "over": sum(g > FRAME_MS for g in gaps),
                "busy": sum(g - 1 for g in gaps if g > 2)}


def rss_mb() -> float:
    """The process's resident memory now (Linux), or its peak elsewhere."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 2**20
    except OSError:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 2**20 if sys.platform == "darwin" else peak / 1024


def mouse(widget, kind, pos: QPointF, buttons=Qt.MouseButton.LeftButton):
    button = Qt.MouseButton.NoButton if kind == QMouseEvent.Type.MouseMove else Qt.MouseButton.LeftButton
    held = buttons if kind != QMouseEvent.Type.MouseButtonRelease else Qt.MouseButton.NoButton
    QApplication.sendEvent(widget, QMouseEvent(kind, pos, widget.mapToGlobal(pos), button, held,
                                               Qt.KeyboardModifier.NoModifier))


def wheel(widget, pos: QPointF, dy: int):
    QApplication.sendEvent(widget, QWheelEvent(pos, widget.mapToGlobal(pos), QPoint(), QPoint(0, dy),
                                               Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                               Qt.ScrollPhase.NoScrollPhase, False))


def gestures(w) -> dict:
    """Each interaction as a list of steps a frame apart."""
    def chart():
        return w.overview.timeline

    def plot():
        return chart()._areas()[0]

    def nav():
        return chart()._areas()[1]

    def zoom_in():
        r = plot()
        return [lambda: wheel(chart(), QPointF(r.center()), 120) for _ in range(20)]

    def zoom_out():
        r = plot()
        return [lambda: wheel(chart(), QPointF(r.center()), -120) for _ in range(20)]

    def pan():
        r = nav()
        c = chart()
        lo, hi = c.view or c.domain()
        a, b = c.domain()
        x0 = c._x((lo + hi) / 2, a, b, r)
        y = r.center().y()
        steps = [lambda: mouse(chart(), QMouseEvent.Type.MouseButtonPress, QPointF(x0, y))]
        steps += [lambda i=i: mouse(chart(), QMouseEvent.Type.MouseMove, QPointF(x0 - 4 * i, y)) for i in range(1, 40)]
        steps.append(lambda: mouse(chart(), QMouseEvent.Type.MouseButtonRelease, QPointF(x0 - 160, y)))
        return steps

    def select():
        r = plot()
        y = r.center().y()
        x0 = r.left() + r.width() * 0.3
        steps = [lambda: mouse(chart(), QMouseEvent.Type.MouseButtonPress, QPointF(x0, y))]
        steps += [lambda i=i: mouse(chart(), QMouseEvent.Type.MouseMove, QPointF(x0 + 8 * i, y)) for i in range(1, 30)]
        steps.append(lambda: mouse(chart(), QMouseEvent.Type.MouseButtonRelease, QPointF(x0 + 240, y)))
        return steps

    def hover():
        r = plot()
        return [lambda i=i: mouse(chart(), QMouseEvent.Type.MouseMove, QPointF(r.left() + 10 * i, r.center().y()),
                                  Qt.MouseButton.NoButton) for i in range(1, 60)]

    def presets():
        return [lambda k=k: chart().zoom_preset(k) for k in ("1m", "3m", "1y", "All")]

    return {"hover": hover, "zoom in": zoom_in, "zoom out": zoom_out, "pan": pan, "drag-select": select,
            "range buttons": presets}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scale", type=float, default=1.0, help="times the owner's subreddit size")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--only", default="", help="run only interactions whose name has this in it")
    parser.add_argument("--repaints", action="store_true", help="time repainting the window and its parts")
    parser.add_argument("--stalls", action="store_true", help="say where the GUI thread was in each long gap")
    parser.add_argument("--sort", default="cumulative", help="profile order: cumulative or tottime")
    parser.add_argument("--profile", help="print where the time goes in interactions whose name has this in it")
    parser.add_argument("--db", help="a copy of this database instead of made-up data (the file isn't changed)")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="ikari-perf-"))
    db = str(tmp / "perf.db")
    t = time.perf_counter()
    if args.db:
        shutil.copyfile(args.db, db)
    else:
        seed(db, "Deltarune", args.scale, 1)
        seed(db, "linux_gaming", args.scale * 0.5, 2)
    store = Store(db)
    biggest, second = (sorted(store.datasets(), key=lambda d: -d["posts"] - d["comments"]) * 2)[:2]
    store.close()
    print(f"seeded in {time.perf_counter() - t:.1f}s")

    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ikari-perf")
    app.setApplicationName(tmp.name)
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp / "cfg"))
    QSettings().setValue("updates/auto", False)
    from subsentiment.gui import theme
    from subsentiment.gui.main_window import MainWindow
    theme.load_fonts(app)
    theme.apply(app, "light")  # as the app does before its window
    QSettings().setValue("ui/last_dataset", biggest["name"])
    beat = Heartbeat()
    if args.stalls:
        beat.sample()
    t = time.perf_counter()
    MainWindow.wait_for_data = False  # as the app opens it
    w = MainWindow(db)
    w.resize(1280, 760)
    w.show()
    opened = time.perf_counter() - t
    worst = 0.0
    while True:  # until the subreddit is in and counted
        beat.run([], 500)
        worst = max(worst, beat.worst_ms)
        if (w.current == biggest["name"] and not w._wanted and not w._span_slices.running() and w.word_tally.ready
                and not w.upgrading.is_alive()):
            break
    ready = time.perf_counter() - t
    beat.worst_ms = worst
    print(f"dpr {w.devicePixelRatioF()}, {len(w._saved['posts']):,} posts, {len(w._saved['comments']):,} comments")
    print(f"window up in {opened * 1000:.0f} ms, ready in {ready * 1000:.0f} ms (worst gap {beat.worst_ms:.0f} ms); "
          f"memory {rss_mb():.0f} MB")
    print(f"{'interaction':<34}{'worst ms':>9}{'>16.7ms':>9}{'busy ms':>9}")

    def report(name, steps):
        if args.only not in name:
            return
        for _ in range(args.repeat):
            prof = cProfile.Profile() if args.profile and args.profile in name else None
            if prof:
                prof.enable()
            beat.stalls = []
            r = beat.run(steps)
            for ms, where in beat.stalls:
                print(f"    {ms:6.0f} ms  " + " | ".join(f"{n}x {w}" for w, n in where))
            if prof:
                prof.disable()
                pstats.Stats(prof).sort_stats(args.sort).print_stats(45)
            print(f"{name:<34}{r['worst']:>9.1f}{r['over']:>9}{r['busy']:>9.0f}")

    if args.repaints:
        for page in ("overview", "words"):
            w.show_page(page)
            beat.run([], 800)
            for widget in (w, w.overview.timeline, w.overview.tiles["count"], w.words_page.body,
                           w.rail):
                if widget.isVisible():
                    t = time.perf_counter()
                    for _ in range(10):
                        widget.repaint()
                    print(f"{page}: repaint {type(widget).__name__:<14}{(time.perf_counter() - t) * 100:7.1f} ms")
        return 0
    w.show_page("overview")
    beat.run([], 800)
    for name, make in gestures(w).items():
        report(f"overview: {name}", make())
    for page in ("words", "posts", "overview", "comments", "overview"):
        report(f"open page {page}", [lambda p=page: w.show_page(p)])
    for page in ("overview", "words"):
        w.show_page(page)
        beat.run([], 800)
        for name in (second["name"], biggest["name"]):  # as a click in the Saved list does it
            row = next(i for i in range(w.datasets.count()) if w.datasets.item(i).data(Qt.ItemDataRole.UserRole) == name)
            report(f"{page}: switch to {name}", [lambda r=row: w.datasets.setCurrentRow(r)])
    for page in ("overview", "words"):
        # A fetch saving comments, a request's 100 at a time: the word clouds follow every 250 ms (the overview's
        # refresh) for two seconds.
        w.show_page(page)
        half = len(w.comments) // 2
        w.word_tally.count(w.posts, w.comments[:half])
        beat.run([], 3000)
        rest = w.comments[half:]
        batch = 100
        report(f"{page}: words during a fetch",
               [(lambda b=rest[i // 15 * batch:(i // 15 + 1) * batch]: (w.word_tally.add("comments", b), w._render_words()))
                if i % 15 == 0 else (lambda: None) for i in range(120)])
    for page in ("overview", "words", "comments"):
        w.show_page(page)
        beat.run([], 800)
        for key in ("dark", "light"):  # as View > Theme does it
            report(f"{page}: theme {key}", [lambda k=key: w._set_theme(k)])
    w.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
