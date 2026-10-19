"""One fetch job: pull posts (and optionally comments), score them, store them."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import requests

from subsentiment import sentiment
from subsentiment.arctic_shift import ArcticShiftClient
from subsentiment.reddit import FetchError, Post, normalize_subreddit
from subsentiment.store import Store

GIVE_UP_AFTER = 5  # consecutive posts whose comments couldn't be fetched before the rest are skipped


@dataclass
class FetchRequest:
    subreddit: str
    after: float | None = None  # epoch seconds, inclusive
    before: float | None = None  # epoch seconds, exclusive
    limit: int = 100
    query: str = ""
    include_comments: bool = True
    comments_per_post: int = 50
    top: bool = False  # highest-scored posts of each part of the range, instead of an even sample
    refresh_scores: bool = False  # also re-check saved posts whose scores hadn't settled


@dataclass
class FetchResult:
    subreddit: str
    posts: int  # new posts saved
    comments: int
    stopped: bool
    updated: int = 0  # saved posts whose scores were re-fetched
    already: int = 0  # posts picked that were already saved with settled scores
    comment_errors: int = 0  # posts whose comments couldn't be fetched (or were skipped after errors)


def run(client: ArcticShiftClient, store: Store, req: FetchRequest,
        progress: Callable[[int, int, str], None] = lambda done, total, msg: None,
        should_stop: Callable[[], bool] = lambda: False,
        saved: Callable[[], None] = lambda: None) -> FetchResult:
    """Run one fetch. ``saved`` is called after each batch of rows is committed, so a UI can show
    them while the fetch goes on."""
    note = lambda msg: progress(0, 0, msg)  # noqa: E731
    limiter = getattr(client, "limiter", None)
    if limiter is not None:
        limiter.on_wait = lambda secs, why: note(f"{why}. Waiting {secs:.0f} s before the next request…")

    name = normalize_subreddit(req.subreddit)
    stored = {r["id"]: r for r in store.posts_between(name, req.after, req.before)}

    # 1. If asked, saved posts from this range whose scores weren't settled yet: re-fetch them in batches.
    stale = [pid for pid, r in stored.items() if r["score"] is None] if req.refresh_scores else []
    updated: list[Post] = []
    if stale:
        note(f"Checking {len(stale):,} saved posts for updated scores…")
        for p in client.fetch_posts_by_ids(stale, should_stop=should_stop):
            if p.score is not None:
                store.save_post(p, *sentiment.score(p.text))
                updated.append(p)
        store.commit()
        saved()

    # 2. New posts. An even sample skips everything already saved, so a repeat fetch adds new posts;
    #    top mode ranks everything, then keeps the ones that aren't saved yet.
    picked = client.fetch_posts(name, req.after, req.before, req.limit, req.query, top=req.top,
                                skip=() if req.top else stored.keys(),
                                progress=note, should_stop=should_stop)
    new = [p for p in picked if p.id not in stored]
    for p in new:
        store.save_post(p, *sentiment.score(p.text))
    store.commit()
    saved()
    first = (new or updated or picked or [None])[0]
    subreddit = first.subreddit if first else name  # canonical capitalisation from Reddit

    # 3. Comments: for new posts, for saved ones whose scores just settled (their comment scores have
    #    too), and for saved posts whose comments were never fetched or failed last time. A post whose
    #    comments came back fine, even if there were none, isn't asked about again.
    n_comments = errors = 0
    if req.include_comments and not should_stop():
        refreshed = {p.id for p in updated}
        todo = new + updated + [_stored_post(r) for pid, r in stored.items()
                                if pid not in refreshed and _comments_missing(r)]
        todo = [p for p in todo if p.num_comments != 0]  # None (not settled yet) still gets looked up
        failed_in_row = 0
        for i, p in enumerate(todo, 1):
            if should_stop():
                break
            progress(i - 1, len(todo), f"Comments for post {i}/{len(todo)}: {p.title[:60]}")
            try:
                comments = client.fetch_comments(p, req.comments_per_post)
            except (FetchError, requests.RequestException):
                store.mark_comments(p.id, ok=False, settled=False)
                store.commit()
                errors += 1
                failed_in_row += 1
                if failed_in_row >= GIVE_UP_AFTER:  # the archive is struggling; don't hammer it
                    errors += len(todo) - i
                    break
                continue
            failed_in_row = 0
            for c in comments:
                store.save_comment(c, *sentiment.score(c.body))
                n_comments += 1
            store.mark_comments(p.id, ok=True, settled=p.score is not None)
            store.commit()
            if comments:
                saved()
        if todo:
            progress(len(todo), len(todo), "Done")
    return FetchResult(subreddit, len(new), n_comments, should_stop(), updated=len(updated),
                       already=len(picked) - len(new), comment_errors=errors)


def _comments_missing(r) -> bool:
    if r["comments_ok"] is None:  # never asked (or saved by an older version, before this was tracked)
        return not r["has_comments"]
    return not r["comments_ok"]


def _stored_post(r) -> Post:
    return Post(r["id"], r["subreddit"], r["title"] or "", r["selftext"] or "", r["author"] or "", r["score"],
                r["upvote_ratio"], r["num_comments"], r["created_utc"] or 0.0, r["permalink"] or "", r["url"] or "")
