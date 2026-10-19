"""One fetch job: pull posts (and optionally comments), score them, store them."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from subsentiment import sentiment
from subsentiment.sources import Source
from subsentiment.store import Store


@dataclass
class FetchRequest:
    subreddit: str
    sort: str = "hot"
    time_filter: str = "week"
    limit: int = 100
    query: str = ""
    include_comments: bool = False
    comments_per_post: int = 50


@dataclass
class FetchResult:
    subreddit: str
    posts: int
    comments: int
    stopped: bool


def run(client: Source, store: Store, req: FetchRequest,
        progress: Callable[[int, int, str], None] = lambda done, total, msg: None,
        should_stop: Callable[[], bool] = lambda: False) -> FetchResult:
    posts = client.fetch_posts(req.subreddit, req.sort, req.time_filter, req.limit, req.query,
                               progress=lambda msg: progress(0, 0, msg), should_stop=should_stop)
    if not posts:
        return FetchResult(req.subreddit, 0, 0, should_stop())
    subreddit = posts[0].subreddit  # canonical capitalisation from Reddit
    for p in posts:
        store.save_post(p, *sentiment.score(p.text))
    store.commit()

    n_comments = 0
    if req.include_comments:
        for i, p in enumerate(posts, 1):
            if should_stop():
                break
            progress(i - 1, len(posts), f"Comments for post {i}/{len(posts)}: {p.title[:60]}")
            if p.num_comments == 0:
                continue
            for c in client.fetch_comments(p, req.comments_per_post):
                store.save_comment(c, *sentiment.score(c.body))
                n_comments += 1
            store.commit()
        progress(len(posts), len(posts), "Done")
    return FetchResult(subreddit, len(posts), n_comments, should_stop())
