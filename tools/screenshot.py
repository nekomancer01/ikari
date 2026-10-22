"""Render the README screenshots from made-up sample data.

    QT_QPA_PLATFORM=offscreen python tools/screenshot.py          # writes docs/*.png
    python tools/screenshot.py --out some/folder

Seeds a throwaway database with invented posts and comments for r/riverton (tools/sample_text.py), then grabs the main
window in light and dark mode. Your own saved data and settings are not touched. Needs PySide6
and the app installed (pip install -e ".[dev]"), plus a display or QT_QPA_PLATFORM=offscreen.
"""
from __future__ import annotations

import argparse
import os
import random
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QSettings, Qt
from PySide6.QtWidgets import QApplication

from subsentiment import sentiment
from subsentiment.arctic_shift import SETTLE_SECONDS
from subsentiment.gui import motion, native, theme, worker
from subsentiment.gui.main_window import MainWindow
from subsentiment.reddit import Comment, Post
from subsentiment.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sample_text  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SUBREDDIT = "riverton"  # a made-up city; see tools/sample_text.py
USERS = ["tram_fan_88", "mill_st_local", "quietcommuter", "rivertonmum", "bikes_not_cars",
         "old_bridge_photos", "night_owl_42", "canal_road_res", "dumpling_hunter", "council_watcher"]
PAGES = ["overview", "words", "posts", "comments", "coverage"]
SHOTS = [(f"{page}-{mode}.png", mode, page) for page in PAGES for mode in ("light", "dark")]
SIZE = (1280, 800)  # logical; at the 1.5 device pixel ratio set below that is 1920x1200 pixels

# Share of positive / negative / neutral posts in each phase of the made-up story.
PHASES = [  # (first day, last day, weights)
    (0, 11, {"positive": 6, "negative": 2, "neutral": 3}),   # the tram line opens
    (12, 21, {"positive": 1, "negative": 7, "neutral": 3}),  # it keeps breaking down
    (22, 34, {"positive": 5, "negative": 3, "neutral": 3}),  # fixed, mood recovers
]
DAYS = 35


def tone_for(day: int, rng: random.Random) -> str:
    weights = next(w for first, last, w in PHASES if first <= day <= last)
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def seed(path: str) -> None:
    rng = random.Random(7)  # fixed seed: the same screenshots every run
    titles = sample_text.Titles(rng)
    store = Store(path)
    start = time.time() - DAYS * 86400
    for i in range(320):
        day = rng.randrange(DAYS)
        tone = tone_for(day, rng)
        title = titles.take(tone)
        score, label = sentiment.score(title)
        created = start + (day + rng.uniform(0.1, 0.9)) * 86400
        comments = []
        for j in range(rng.randint(0, 3)):
            # replies mostly share the post's mood
            text = sample_text.comment(tone if rng.random() < 0.8 else tone_for(day, rng), rng)
            c_score, c_label = sentiment.score(text)
            comment = Comment(id=f"c{i:04d}{j}", post_id=f"p{i:04d}", subreddit=SUBREDDIT,
                              parent_id=f"t3_p{i:04d}", author=rng.choice(USERS),
                              body=text, score=rng.randint(0, 200) if created < time.time() - SETTLE_SECONDS else None,
                              created_utc=created + 1800 * (j + 1),
                              depth=0, permalink=f"/r/{SUBREDDIT}/comments/p{i:04d}/c{j}/")
            comments.append((comment, c_score, c_label))
        settled = created < time.time() - SETTLE_SECONDS  # the archive hasn't re-fetched newer posts yet
        post = Post(id=f"p{i:04d}", subreddit=SUBREDDIT, title=title, selftext="",
                    author=rng.choice(USERS), score=rng.randint(1, 900) if settled else None,
                    upvote_ratio=round(rng.uniform(0.6, 0.98), 2) if settled else None,
                    num_comments=len(comments) if settled else None,
                    created_utc=created, permalink=f"/r/{SUBREDDIT}/comments/p{i:04d}/",
                    url=f"https://example.com/p{i:04d}")
        store.save_post(post, score, label)
        for comment, c_score, c_label in comments:
            store.save_comment(comment, c_score, c_label)
    store.commit()
    store.close()


class CountingArchive:
    """Answers the Coverage page's day counts: a little more than saved on most days, so some read as partial."""

    def __init__(self, *args, **kwargs):
        self.limiter = type("L", (), {"on_wait": None})()

    def day_counts(self, subreddit, after, before, should_stop=None):
        rng = random.Random(3)
        return {d: rng.choice([13, 14, 15, 16, 18, 20]) for d in range(int(after // 86400 * 86400), int(before), 86400)}


def settle(app, seconds: float = 0.4) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.02)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "docs"), help="folder for the PNG files")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    db = Path(tempfile.mkdtemp(prefix="ikari-shots-")) / "sample.db"
    seed(str(db))

    os.environ.setdefault("QT_SCALE_FACTOR", "1.5")  # a real 1.5 device pixel ratio, as on the owner's screen
    worker.ArcticShiftClient = CountingArchive  # the Coverage page asks the archive for day totals
    native.setup_app_identity()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ikari-screenshots")  # keeps this run's settings separate from the user's
    app.setApplicationName("ikari-screenshots")
    app.setApplicationDisplayName("ikari")
    settings = QSettings()
    settings.setValue("updates/auto", False)  # no update check while taking screenshots
    theme.load_fonts(app)

    window = MainWindow(str(db))
    motion.set_enabled(False)  # stills of the finished state, not mid-fade
    window.resize(*SIZE)
    window.fetch_form.range.setCurrentIndex(window.fetch_form.range.findData("month"))
    window.show()
    window._refresh_datasets(SUBREDDIT)
    window.fetch_form.subreddit.setText(SUBREDDIT)
    window.fetch_form.include_comments.setChecked(True)
    window.rail.setFocus()  # no text cursor blinking in the subreddit box
    window.scope.setCurrentText("Posts only")  # comments are short and repeat; post titles don't
    for table in (window.posts_tab, window.comments_tab):  # newest first shows a mix of labels
        table.view.sortByColumn(6, Qt.SortOrder.DescendingOrder)

    for name, mode, page in SHOTS:
        settings.setValue("ui/theme", mode)
        window._apply_theme()
        window.show_page(page)
        if page == "coverage":
            settle(app, 1.5)  # the archive's day counts arrive
            cov = window.coverage
            cov.day_table.selectRow(3)
            cov.ticked = set(cov.days[2:4])
            cov._show_ticked()
        settle(app, 1.5 if page in ("coverage", "words", "overview") else 0.3)  # counts, word tallies, charts
        path = out / name
        window.grab().save(str(path))
        print("wrote", path)

    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
