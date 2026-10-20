"""One fetch job: pull posts (and optionally comments), score them, store them."""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable

import requests

from subsentiment import sentiment
from subsentiment.arctic_shift import ArcticShiftClient, Stopped, in_parallel
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
    # Every post in [after, before), with limit as a cap; when the archive runs out first, the range
    # (one UTC day, starting at after) is recorded as fully downloaded.
    everything: bool = False
    # Only fetch every comment on these saved posts; the other fields but subreddit are ignored.
    comment_posts: tuple[str, ...] = ()


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
    if hasattr(client, "should_stop"):
        client.should_stop = should_stop  # so Stop also cuts short retries and waits inside a request

    if req.comment_posts:
        return _all_comments(client, store, req, progress, should_stop, saved)

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
    # Each post is saved and announced the moment it is downloaded, so a UI can show it right away.
    streamed: set[str] = set()

    def save_streamed(batch: list[Post]) -> None:
        for p in batch:
            if p.id not in stored and p.id not in streamed:
                store.save_post(p, *sentiment.score(p.text))
                store.commit()
                streamed.add(p.id)
                saved()

    picked = client.fetch_posts(name, req.after, req.before, req.limit, req.query, top=req.top,
                                skip=() if req.top else stored.keys(),
                                progress=note, should_stop=should_stop, on_posts=save_streamed)
    removed = getattr(client, "removed", None)
    if removed:  # the archive's daily counts include these, so Download coverage counts them as checked
        store.save_removed(name, removed)
        removed.clear()
    new = [p for p in picked if p.id not in stored]
    for p in new:
        if p.id not in streamed:
            store.save_post(p, *sentiment.score(p.text))
    # A day that isn't over yet can still get posts, so it's never recorded as complete.
    if (req.everything and req.after is not None and req.before is not None and req.before <= time.time()
            and not should_stop() and len(new) < req.limit):
        store.mark_complete("posts", name, str(int(req.after)))
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
        repicked = {p.id for p in picked if p.id in stored}  # top mode can pick posts that are already saved
        todo = new + updated + [_stored_post(r) for pid, r in stored.items()
                                if pid not in refreshed and (_comments_missing(r)
                                                             or (pid in repicked and _never_asked(r)))]
        todo = [p for p in todo if p.num_comments != 0]  # None (not settled yet) still gets looked up
        done = 0
        gave_up = threading.Event()
        streak = [0]  # failures in a row, counted as they happen so no new requests start after the limit
        lock = threading.Lock()

        def fetch(p: Post):
            if should_stop() or gave_up.is_set():
                return None  # not tried
            try:
                comments = client.fetch_comments(p, req.comments_per_post)
            except Stopped:
                return None  # not tried
            except (FetchError, requests.RequestException):
                with lock:
                    streak[0] += 1
                    if streak[0] >= GIVE_UP_AFTER:  # the archive is struggling; don't hammer it
                        gave_up.set()
                raise
            with lock:
                streak[0] = 0
            return comments

        # A few posts' comments are fetched at once; each finished one is saved here, on this thread.
        tried: set[str] = set()
        for p, comments, error in in_parallel(fetch, todo, getattr(client, "workers", 1), should_stop):
            if comments is None and error is None:
                continue
            tried.add(p.id)
            done += 1
            progress(done, len(todo), f"Comments for post {done}/{len(todo)}: {p.title[:60]}")
            if error is not None:
                if not isinstance(error, (FetchError, requests.RequestException)):
                    raise error
                store.mark_comments(p.id, ok=False, settled=False)
                store.commit()
                errors += 1
                continue
            for c in comments:
                store.save_comment(c, *sentiment.score(c.body))
                n_comments += 1
            store.mark_comments(p.id, ok=True, settled=p.score is not None)
            store.commit()
            if comments:
                saved()
        if gave_up.is_set():
            rest = [p for p in todo if p.id not in tried]  # not tried this time: retried on the next fetch
            errors += len(rest)
            for p in rest:
                store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
        if todo:
            progress(len(todo), len(todo), "Done")
    return FetchResult(subreddit, len(new), n_comments, should_stop(), updated=len(updated),
                       already=len(picked) - len(new), comment_errors=errors)


def _all_comments(client: ArcticShiftClient, store: Store, req: FetchRequest, progress, should_stop,
                  saved) -> FetchResult:
    """Every comment on each post in req.comment_posts. Posts whose numbers hadn't settled are
    re-checked first, so their comment totals are real where the archive has them by now."""
    name = normalize_subreddit(req.subreddit)
    posts = [_stored_post(r) for r in store.posts_by_ids(req.comment_posts)]
    stale = [p.id for p in posts if p.score is None]
    fresh: dict[str, Post] = {}
    if stale:
        progress(0, 0, f"Checking {len(stale):,} saved posts for updated comment counts…")
        fresh = {p.id: p for p in client.fetch_posts_by_ids(stale, should_stop=should_stop) if p.score is not None}
        for p in fresh.values():
            store.save_post(p, *sentiment.score(p.text))
        store.commit()
        posts = [fresh.get(p.id, p) for p in posts]
    n_comments = errors = done = 0
    gave_up = threading.Event()
    streak = [0]  # failures in a row, counted as they happen so no new requests start after the limit
    lock = threading.Lock()

    def fetch(p: Post):
        if should_stop() or gave_up.is_set():
            return None  # not tried
        try:
            got = client.fetch_all_comments(p)
        except Stopped:
            return None
        except (FetchError, requests.RequestException):
            with lock:
                streak[0] += 1
                if streak[0] >= GIVE_UP_AFTER:
                    gave_up.set()
            raise
        with lock:
            streak[0] = 0
        return got

    # A few posts' threads are fetched at once; each finished one is saved here, on this thread.
    tried: set[str] = set()
    for p, got, error in in_parallel(fetch, posts, getattr(client, "workers", 1), should_stop):
        if got is None and error is None:
            continue
        tried.add(p.id)
        done += 1
        progress(done, len(posts), f"All comments for post {done}/{len(posts)}: {p.title[:60]}")
        if error is not None:
            if not isinstance(error, (FetchError, requests.RequestException)):
                raise error
            store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
            errors += 1
            continue
        comments, whole = got
        for c in comments:
            store.save_comment(c, *sentiment.score(c.body))
            n_comments += 1
        store.mark_comments(p.id, ok=True, settled=p.score is not None)
        if whole and p.score is not None:  # an unsettled thread may still be growing
            store.mark_complete("comments", name, p.id)
        store.commit()
        saved()
    if gave_up.is_set():
        errors += len([p for p in posts if p.id not in tried])  # not tried: retried next time
    if posts:
        progress(len(posts), len(posts), "Done")
    subreddit = posts[0].subreddit if posts else name
    return FetchResult(subreddit, 0, n_comments, should_stop(), updated=len(fresh), comment_errors=errors)


def _comments_missing(r) -> bool:
    # Only a post whose comments were asked for and failed is retried. Saved posts nobody asked comments
    # for are left alone: a fetch must not quietly download comments for posts it wasn't fetching.
    return r["comments_ok"] is not None and not r["comments_ok"]


def _never_asked(r) -> bool:
    return r["comments_ok"] is None and not r["has_comments"]


def _stored_post(r) -> Post:
    return Post(r["id"], r["subreddit"], r["title"] or "", r["selftext"] or "", r["author"] or "", r["score"],
                r["upvote_ratio"], r["num_comments"], r["created_utc"] or 0.0, r["permalink"] or "", r["url"] or "")
