"""One fetch job: pull posts (and optionally comments), score them, store them."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Collection

import requests

from subsentiment import sentiment
from subsentiment.arctic_shift import ArcticShiftClient, Stopped, in_parallel
from subsentiment.reddit import FetchError, Post, normalize_subreddit
from subsentiment.store import Store

GIVE_UP_AFTER = 5  # consecutive posts whose comments couldn't be fetched before the rest are skipped
DAY = 86400
MAX_DAY_POSTS = 10000  # cap on one UTC day when downloading everything (busy subreddits post ~4,500 a day)


@dataclass
class FetchRequest:
    subreddit: str
    after: float | None = None  # epoch seconds, inclusive
    before: float | None = None  # epoch seconds, exclusive
    # How many posts to pick; None means every post in the range, downloaded one UTC day at a time like
    # Download coverage's "Download all", so each past day read to its end is recorded as complete.
    limit: int | None = 100
    query: str = ""
    include_comments: bool = True
    comments_per_post: int = 50
    top: bool = False  # highest-scored posts of each part of the range, instead of an even sample
    refresh_scores: bool = False  # also re-check saved posts whose scores hadn't settled
    # Every post in [after, before), with limit as a cap; when every part of the range was read to its
    # end, the range (one UTC day, starting at after) is recorded as fully downloaded.
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
    timed_out: bool = False  # part of the range was skipped: the archive kept timing out on it


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

    def read(after, before, limit: int, everything: bool, say, whole_day: bool = True,
             skip: Collection[str] = stored.keys()) -> list[Post]:
        got = client.fetch_posts(name, after, before, limit, req.query, top=req.top,
                                 skip=() if req.top else skip,
                                 progress=say, should_stop=should_stop, on_posts=save_streamed,
                                 exhaust=everything)
        # Taken, not read and cleared: after Stop, slice threads still running may be adding to it.
        take = getattr(client, "take_removed", None)
        removed = take() if take else {}
        if removed:  # the archive's daily counts include these, so Download coverage counts them as checked
            store.save_removed(name, removed)
        # Complete only when every part of the day was read to its end: a fetch that stopped at the limit, or
        # was stopped, may have missed posts. A day that isn't over yet can still get posts, so it never is,
        # and a keyword search only reads the posts that match.
        if (everything and whole_day and after is not None and before is not None and before <= time.time()
                and not req.query and not should_stop() and getattr(client, "exhausted", False)):
            store.mark_complete("posts", name, str(int(after)))
        return got

    if req.limit is None:
        picked = []
        days = _days(name, store, req.after, req.before)
        for k, (lo, hi) in enumerate(days):
            if should_stop():
                break
            whole = hi - lo == DAY and lo % DAY == 0
            when = time.strftime("%a %d %b", time.gmtime(lo))

            def say(_msg, k=k, when=when):
                # Short, as the window shows one line: the bar counts days, this says which and how many.
                progress(k, len(days), f"Day {k + 1} of {len(days)}, {when}: {len(streamed):,} new posts")
            say("")
            saved_that_day = [pid for pid, r in stored.items() if lo <= (r["created_utc"] or 0) < hi]
            picked += read(lo, hi, MAX_DAY_POSTS, True, say, whole_day=whole, skip=saved_that_day)
        if days:
            progress(len(days), len(days), f"{len(streamed):,} new posts from r/{name}")
    else:
        picked = read(req.after, req.before, req.limit, req.everything, note)
    new = [p for p in picked if p.id not in stored]
    for p in new:
        if p.id not in streamed:
            store.save_post(p, *sentiment.score(p.text))
    # Top mode can pick a saved post whose score has come in since: it downloaded the settled copy, so
    # keep it (it used to be dropped, leaving the score blank unless "Update saved posts" was ticked).
    rechecked = {p.id for p in updated}
    settled = [p for p in picked if p.id in stored and p.id not in rechecked
               and stored[p.id]["score"] is None and p.score is not None]
    for p in settled:
        store.save_post(p, *sentiment.score(p.text))
    updated += settled
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

        def save(p: Post, comments) -> None:
            nonlocal n_comments
            for c in comments:
                store.save_comment(c, *sentiment.score(c.body))
                n_comments += 1
            store.mark_comments(p.id, ok=True, settled=p.score is not None)
            store.commit()
            if comments:
                saved()

        errors, tried, gave_up = _each_thread(
            todo, lambda p: client.fetch_comments(p, req.comments_per_post), save, "Comments for post",
            store, getattr(client, "workers", 1), progress, should_stop)
        if gave_up:
            rest = [p for p in todo if p.id not in tried]  # not tried this time: retried on the next fetch
            errors += len(rest)
            for p in rest:
                store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
        if todo:
            progress(len(todo), len(todo), "Done")
    return FetchResult(subreddit, len(new), n_comments, should_stop(), updated=len(updated),
                       already=len(picked) - len(new) - len(settled), comment_errors=errors,
                       timed_out=bool(getattr(client, "timed_out", False)))


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
    n_comments = 0

    def save(p: Post, got) -> None:
        nonlocal n_comments
        comments, whole = got
        for c in comments:
            store.save_comment(c, *sentiment.score(c.body))
            n_comments += 1
        store.mark_comments(p.id, ok=True, settled=p.score is not None)
        if whole:  # an unsettled thread may still be growing: it has every comment so far, not all
            store.mark_complete("comments" if p.score is not None else "comments_so_far", name, p.id)
        store.commit()
        saved()

    errors, tried, gave_up = _each_thread(posts, client.fetch_all_comments, save, "All comments for post",
                                          store, getattr(client, "workers", 1), progress, should_stop)
    if gave_up:
        errors += len([p for p in posts if p.id not in tried])  # not tried: retried next time
    if posts:
        progress(len(posts), len(posts), "Done")
    subreddit = posts[0].subreddit if posts else name
    return FetchResult(subreddit, 0, n_comments, should_stop(), updated=len(fresh), comment_errors=errors)


def _each_thread(posts: list[Post], get: Callable, save: Callable, what: str, store: Store, workers: int,
                 progress, should_stop) -> tuple[int, set[str], bool]:
    """Run get(post) for each post, a few at once, and hand each result to save(post, result) here, on
    this thread. A post whose comments couldn't be fetched is recorded as failed, so the next fetch retries
    it; after GIVE_UP_AFTER failures in a row no new ones start, since the archive is struggling and
    shouldn't be hammered. Returns (posts that failed, ids of the posts tried, whether it gave up)."""
    gave_up = threading.Event()
    streak = [0]  # failures in a row, counted as they happen so no new requests start after the limit
    lock = threading.Lock()

    def fetch(p: Post):
        if should_stop() or gave_up.is_set():
            return None  # not tried
        try:
            got = get(p)
        except Stopped:
            return None  # not tried
        except (FetchError, requests.RequestException):
            with lock:
                streak[0] += 1
                if streak[0] >= GIVE_UP_AFTER:
                    gave_up.set()
            raise
        with lock:
            streak[0] = 0
        return got

    tried: set[str] = set()
    errors = 0
    for p, got, error in in_parallel(fetch, posts, workers, should_stop):
        if got is None and error is None:
            continue
        tried.add(p.id)
        progress(len(tried), len(posts), f"{what} {len(tried)}/{len(posts)}: {p.title[:60]}")
        if error is not None:
            if not isinstance(error, (FetchError, requests.RequestException)):
                raise error
            store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
            errors += 1
            continue
        save(p, got)
    return errors, tried, gave_up.is_set()


def days_in(after: float, before: float) -> list[int]:
    """UTC day starts overlapping [after, before), newest first."""
    first = int(after // DAY * DAY)
    last = int(math.ceil(before / DAY) * DAY) - DAY
    return list(range(last, first - 1, -DAY))


def _days(name: str, store: Store, after: float | None, before: float | None) -> list[tuple[float, float]]:
    """[after, before) as UTC days, newest first, each clipped to the range; past days already downloaded
    in full are left out, as they hold nothing new."""
    before = time.time() if before is None else before
    after = before - 7 * DAY if after is None else after
    done = store.complete_keys("posts", name)
    return [(max(day, after), min(day + DAY, before)) for day in days_in(after, before)
            if str(day) not in done and min(day + DAY, before) > max(day, after)]


def _comments_missing(r) -> bool:
    # Only a post whose comments were asked for and failed is retried. Saved posts nobody asked comments
    # for are left alone: a fetch must not quietly download comments for posts it wasn't fetching.
    return r["comments_ok"] is not None and not r["comments_ok"]


def _never_asked(r) -> bool:
    return r["comments_ok"] is None and not r["has_comments"]


def _stored_post(r) -> Post:
    return Post(r["id"], r["subreddit"], r["title"] or "", r["selftext"] or "", r["author"] or "", r["score"],
                r["upvote_ratio"], r["num_comments"], r["created_utc"] or 0.0, r["permalink"] or "", r["url"] or "")
