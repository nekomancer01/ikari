"""Render the README screenshots from made-up sample data.

    QT_QPA_PLATFORM=offscreen python tools/screenshot.py          # writes docs/*.png
    python tools/screenshot.py --out some/folder

Seeds a throwaway database with invented posts and comments for r/sample, then grabs the main
window in light and dark mode. Your own saved data and settings are not touched. Needs PySide6
and the app installed (pip install -e ".[dev]"), plus a display or QT_QPA_PLATFORM=offscreen.
"""
from __future__ import annotations

import argparse
import random
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication

from subsentiment import sentiment
from subsentiment.gui import native, theme
from subsentiment.gui.main_window import MainWindow
from subsentiment.reddit import Comment, Post
from subsentiment.store import Store

ROOT = Path(__file__).resolve().parents[1]
SUBREDDIT = "sample"
SHOTS = [  # (file name, theme, tab index: 0 Overview, 1 Posts, 2 Comments)
    ("overview-light.png", "light", 0),
    ("overview-dark.png", "dark", 0),
    ("posts-light.png", "light", 1),
    ("posts-dark.png", "dark", 1),
]

TOPICS = ["the new trail map", "our community garden", "the library's reading app", "the bike lanes",
          "my sourdough starter", "the weekend market", "the robot vacuum", "the local bakery",
          "the hiking group", "the train timetable change", "the board game night", "the herb planter"]
GOOD = ["finally works", "is wonderful", "made my week", "exceeded my expectations", "is a joy to use"]
BAD = ["keeps breaking", "is disappointing", "was a frustrating mess", "let everyone down", "is hard to rely on"]
MEH = ["is okay I guess", "is what you'd expect", "has some quirks", "is fine for now"]


TITLES = ["Honest update: {topic} {tone}", "Question: has anyone tried {topic}?",
          "Quick thought on {topic}", "Photos: {topic}", "Week {n} with {topic}"]


def make_text(rng: random.Random) -> tuple[str, str]:
    """A made-up post title and body whose tone is positive, negative or neutral."""
    topic = rng.choice(TOPICS)
    tone = rng.choices([GOOD, BAD, MEH], weights=[4, 3, 3])[0]
    title = rng.choice(TITLES).format(topic=topic, tone=rng.choice(tone), n=rng.randint(1, 12))
    body = f"We tried {topic} this month. It {rng.choice(tone)}, and most of the group agreed."
    return title, body


def seed(path: str) -> None:
    rng = random.Random(7)  # fixed seed: the same screenshots every run
    store = Store(path)
    now = time.time()
    for i in range(140):
        title, body = make_text(rng)
        score, label = sentiment.score(f"{title}. {body}")
        created = now - rng.uniform(0, 45) * 86400
        comments = []
        for j in range(rng.randint(0, 4)):
            text = make_text(rng)[1]
            c_score, c_label = sentiment.score(text)
            comment = Comment(id=f"c{i:04d}{j}", post_id=f"p{i:04d}", subreddit=SUBREDDIT,
                              parent_id=f"t3_p{i:04d}", author=f"sample_user_{rng.randint(1, 60)}",
                              body=text, score=rng.randint(0, 200), created_utc=created + 3600 * (j + 1),
                              depth=0, permalink=f"/r/{SUBREDDIT}/comments/p{i:04d}/c{j}/")
            comments.append((comment, c_score, c_label))
        post = Post(id=f"p{i:04d}", subreddit=SUBREDDIT, title=title, selftext=body,
                    author=f"sample_user_{rng.randint(1, 60)}", score=rng.randint(1, 900),
                    upvote_ratio=round(rng.uniform(0.6, 0.98), 2), num_comments=len(comments),
                    created_utc=created, permalink=f"/r/{SUBREDDIT}/comments/p{i:04d}/",
                    url=f"https://example.com/p{i:04d}")
        store.save_post(post, score, label)
        for comment, c_score, c_label in comments:
            store.save_comment(comment, c_score, c_label)
    store.commit()
    store.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "docs"), help="folder for the PNG files")
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    db = Path(tempfile.mkdtemp(prefix="ikari-shots-")) / "sample.db"
    seed(str(db))

    native.setup_app_identity()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ikari-screenshots")  # keeps this run's settings separate from the user's
    app.setApplicationName("ikari-screenshots")
    app.setApplicationDisplayName("ikari")
    settings = QSettings()
    settings.setValue("updates/auto", False)  # no update check while taking screenshots
    theme.load_fonts(app)

    window = MainWindow(str(db))
    window.resize(1320, 860)
    window.show()
    window._refresh_datasets(SUBREDDIT)

    for name, mode, tab in SHOTS:
        settings.setValue("ui/theme", mode)
        window._apply_theme()
        window.tabbar.setCurrentIndex(tab)
        for _ in range(5):  # let Qt lay out and repaint before grabbing
            app.processEvents()
            time.sleep(0.05)
        path = out / name
        window.grab().save(str(path))
        print("wrote", path)

    window.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
