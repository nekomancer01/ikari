"""One fetch job: pull posts (and optionally comments), score them, store them."""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Collection

import requests

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
    comments_per_post: int = 50  # 0: every comment on each post, as Download coverage's Download all comments
    comment_pick: str = "top"  # which comments a cap keeps: "top" (highest-scored) or "random" (fixed per post)
    # With a cap, keep asking for a saved post's thread until it has that many comments saved or is whole, as
    # Download coverage's ticked days do; a later run with a bigger cap tops threads up. Without it, a capped
    # fetch asks about each post once. comments_per_post 0 always keeps asking until the thread is whole.
    fill_threads: bool = False
    top: bool = False  # highest-scored posts of each part of the range, instead of an even sample
    refresh_scores: bool = False  # also re-check saved posts whose scores hadn't settled
    # Every post in [after, before), with limit as a cap; when every part of the range was read to its
    # end, the range (one UTC day, starting at after) is recorded as fully downloaded.
    everything: bool = False
    # Only fetch every comment on these saved posts; the other fields but subreddit are ignored.
    comment_posts: tuple[str, ...] = ()
    # With limit None: read exactly these UTC days (day starts, in this order), each in full, even ones already
    # complete, instead of the days of [after, before). Download coverage's "Download selected" sends these.
    days: tuple[int, ...] = ()


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
    shown, said = progress, [(0, 0, "")]

    def progress(done: int, total: int, msg: str) -> None:  # remembered, to come back to after a wait
        said[0] = (done, total, msg)
        shown(done, total, msg)

    def waiting(secs: float, why: str) -> None:
        # Once the pause is over, what the fetch is doing comes back: the wait's line used to stay up until
        # the next progress, a whole slice of a busy day later, so the fetch read as stuck waiting.
        if secs:
            shown(0, 0, f"{why}. Waiting {secs:.0f} s before the next request…")
        elif said[0][2]:
            shown(*said[0])
    note = lambda msg: progress(0, 0, msg)  # noqa: E731
    limiter = getattr(client, "limiter", None)
    if limiter is not None:
        limiter.on_wait = waiting
    if hasattr(client, "should_stop"):
        client.should_stop = should_stop  # so Stop also cuts short retries and waits inside a request

    if req.comment_posts:
        return _all_comments(client, store, req, progress, should_stop, saved)

    name = normalize_subreddit(req.subreddit)
    score = lambda text: store.lexicon(name).score(text)  # noqa: E731 - by the subreddit's own words, as set now
    picked_days = set(req.days)
    stored = {r["id"]: r for r in store.posts_between(name, req.after, req.before)
              if not picked_days or (r["created_utc"] or 0) // DAY * DAY in picked_days}  # not days between picked ones

    # 1. If asked, saved posts from this range whose scores weren't settled yet: re-fetch them in batches.
    stale = [pid for pid, r in stored.items() if r["score"] is None and not r["image"]] if req.refresh_scores else []
    updated: list[Post] = []
    if stale:
        note(f"Checking {len(stale):,} saved posts for updated scores…")
        for p in client.fetch_posts_by_ids(stale, should_stop=should_stop):
            if p.score is not None:
                store.save_post(p, *score(p.text))
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
                store.save_post(p, *score(p.text))
                store.commit()
                streamed.add(p.id)
                saved()

    def read(after, before, limit: int, everything: bool, say, whole_day: bool = True,
             skip: Collection[str] = stored.keys()) -> list[Post]:
        # Where earlier fetches got to, so a stopped download goes on from there. Not for a day that's complete,
        # where Check again looks through all of it for posts the archive has taken in since.
        again = after is not None and str(int(after)) in store.complete_keys("posts", name)
        got = client.fetch_posts(name, after, before, limit, req.query, top=req.top,
                                 skip=() if req.top else skip,
                                 progress=say, should_stop=should_stop, on_posts=save_streamed,
                                 exhaust=everything, read=() if again else store.read_spans(name))
        if reached := getattr(client, "reached", None):
            store.save_read_spans(name, reached)
        # Taken, not read and cleared: after Stop, slice threads still running may be adding to it.
        take = getattr(client, "take_removed", None)
        removed = take() if take else {}
        if removed:  # the archive's daily counts include these, so Download coverage counts them as checked
            store.save_removed(name, removed)
        for p in getattr(client, "take_images", list)():  # same for image posts, kept hidden
            store.save_post(p, None, None)
        store.commit()
        # Complete only when every part of the day was read to its end: a fetch that stopped at the limit, or
        # was stopped, may have missed posts. A day that isn't over yet can still get posts, so it never is,
        # and a keyword search only reads the posts that match.
        if (everything and whole_day and after is not None and before is not None and before <= time.time()
                and not req.query and not should_stop() and getattr(client, "exhausted", False)):
            store.mark_complete("posts", name, str(int(after)))
        return got

    # 3. Comments: for new posts, for saved ones whose scores just settled (their comment scores have
    #    too), and for saved posts whose comments were never fetched or failed last time. A post whose
    #    comments came back fine, even if there were none, isn't asked about again. Asked for all comments,
    #    every saved post in the range is, until its whole thread is saved (as Download all comments does);
    #    with fill_threads and a cap, until it has that many saved.
    n_comments = errors = 0
    cap = req.comments_per_post
    fill = req.fill_threads or not cap
    saved_whole = store.complete_keys("comments", name) if fill and req.include_comments else set()
    saved_count = store.comment_counts(name, req.after, req.before) if fill and cap and req.include_comments else {}
    gave_up = False  # the archive kept failing comment requests; the rest are left for the next fetch

    def comments(picked: list[Post], updated: list[Post], in_part: Callable[[str], bool], report) -> None:
        """Comments for one part of the fetch: its new and re-checked posts, and its saved posts as above.
        ``in_part`` says whether a saved post's id belongs to this part."""
        nonlocal n_comments, errors, gave_up
        if not req.include_comments or should_stop():
            return
        refreshed = {p.id for p in updated}
        repicked = {p.id for p in picked if p.id in stored}  # top mode can pick posts that are already saved
        todo = [p for p in picked if p.id not in stored] + updated + [
            _stored_post(r) for pid, r in stored.items()
            if in_part(pid) and not r["image"] and pid not in refreshed and (
                _comments_missing(r) or (pid in repicked and _never_asked(r))
                or (fill and pid not in saved_whole and not (cap and saved_count.get(pid, 0) >= cap)))]
        todo = [p for p in todo if p.num_comments != 0]  # None (not settled yet) still gets looked up
        if gave_up:
            tried: set[str] = set()
        else:
            def save(p: Post, got) -> None:
                nonlocal n_comments
                comments, complete = got if fill else (got, False)
                had = store.comment_ids(p.id)  # a thread asked for again: count only what's new
                for c in comments:
                    store.save_comment(c, *score(c.body))
                    n_comments += c.id not in had
                store.mark_comments(p.id, ok=True, settled=p.score is not None)
                if complete:  # an unsettled thread may still be growing, so it's whole only "so far"
                    store.mark_complete("comments" if p.score is not None else "comments_so_far", name, p.id)
                store.commit()
                if comments:
                    saved()

            get = (lambda p: client.fetch_all_comments(p, cap, req.comment_pick)) if fill else (
                lambda p: client.fetch_comments(p, cap, req.comment_pick))
            failed, tried, gave_up = _each_thread(
                todo, get, save, "Comments for post" if cap else "All comments for post",
                store, getattr(client, "tree_workers", getattr(client, "workers", 1)), report, should_stop)
            errors += failed
        if gave_up:
            rest = [p for p in todo if p.id not in tried]  # not tried this time: retried on the next fetch
            errors += len(rest)
            for p in rest:
                store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
        if todo:
            report(len(todo), len(todo), "Done")

    def settle(picked: list[Post]) -> tuple[list[Post], list[Post]]:
        """Saves what a part's read picked that the stream didn't, and returns (new, settled): top mode can
        pick a saved post whose score has come in since, and that settled copy is kept (it used to be dropped,
        leaving the score blank unless "Update saved posts" was ticked)."""
        new = [p for p in picked if p.id not in stored]
        for p in new:
            if p.id not in streamed:
                store.save_post(p, *score(p.text))
        settled = [p for p in picked if p.id in stored and p.id not in rechecked
                   and stored[p.id]["score"] is None and p.score is not None]
        for p in settled:
            store.save_post(p, *score(p.text))
        store.commit()
        saved()
        return new, settled

    rechecked = {p.id for p in updated}
    picked: list[Post] = []
    new: list[Post] = []
    settled: list[Post] = []
    if req.limit is None:
        # A day at a time, newest first: each day's posts, then their comments, so a long fetch fills in as
        # it goes and Stop keeps whole days.
        days = [(d, d + DAY) for d in req.days] if req.days else _days(name, store, req.after, req.before)
        seen: set[str] = set()  # saved posts on the days read, whose comments those days took care of
        for k, (lo, hi) in enumerate(days):
            if should_stop():
                break
            whole = hi - lo == DAY and lo % DAY == 0
            when = time.strftime("%a %d %b", time.gmtime(lo))

            def say(_msg, k=k, when=when):
                # Short, as the window shows one line: the bar counts days, this says which and how many.
                progress(k, len(days), f"Day {k + 1} of {len(days)}, {when}: {len(streamed):,} new posts")

            def report(done, total, _msg, k=k, when=when):
                progress(k, len(days), f"Day {k + 1} of {len(days)}, {when}: comments on {done:,} of "
                                       f"{total:,} posts")
            say("")
            in_day = {pid for pid, r in stored.items() if lo <= (r["created_utc"] or 0) < hi}
            seen |= in_day
            got = read(lo, hi, MAX_DAY_POSTS, True, say, whole_day=whole, skip=in_day)
            day_new, day_settled = settle(got)
            picked += got
            new += day_new
            settled += day_settled
            day_updated = [p for p in updated if lo <= p.created_utc < hi] + day_settled
            comments(got, day_updated, in_day.__contains__, report)
        # Days already downloaded in full aren't read again, but their saved posts can still be owed comments.
        comments([], [p for p in updated if p.id not in seen], lambda pid: pid not in seen, progress)
        if days:
            progress(len(days), len(days), f"{len(streamed):,} new posts from r/{name}")
    else:
        picked = read(req.after, req.before, req.limit, req.everything, note)
        new, settled = settle(picked)
        comments(picked, updated + settled, lambda pid: True, progress)
    updated += settled
    first = (new or updated or picked or [None])[0]
    subreddit = first.subreddit if first else name  # canonical capitalisation from Reddit
    return FetchResult(subreddit, len(new), n_comments, should_stop(), updated=len(updated),
                       already=len(picked) - len(new) - len(settled), comment_errors=errors,
                       timed_out=bool(getattr(client, "timed_out", False)))


def _all_comments(client: ArcticShiftClient, store: Store, req: FetchRequest, progress, should_stop,
                  saved) -> FetchResult:
    """Every comment on each post in req.comment_posts. Posts whose numbers hadn't settled are
    re-checked first, so their comment totals are real where the archive has them by now."""
    name = normalize_subreddit(req.subreddit)
    score = lambda text: store.lexicon(name).score(text)  # noqa: E731
    posts = [_stored_post(r) for r in store.posts_by_ids(req.comment_posts)]
    stale = [p.id for p in posts if p.score is None]
    fresh: dict[str, Post] = {}
    if stale:
        progress(0, 0, f"Checking {len(stale):,} saved posts for updated comment counts…")
        fresh = {p.id: p for p in client.fetch_posts_by_ids(stale, should_stop=should_stop) if p.score is not None}
        for p in fresh.values():
            store.save_post(p, *score(p.text))
        store.commit()
        posts = [fresh.get(p.id, p) for p in posts]
    if len(posts) > 1:
        # A day's Download all comments offers posts whose comments weren't counted yet; once re-checked, the ones
        # with none, or with every one saved already, cost no request. They're done, so they aren't offered again.
        # One post asked about on its own (its Check again) is asked whatever its count says: a count can lag.
        empty = [p for p in posts if p.num_comments is not None and len(store.comment_ids(p.id)) >= p.num_comments]
        for p in empty:
            store.mark_comments(p.id, ok=True, settled=p.score is not None)
            store.mark_complete("comments" if p.score is not None else "comments_so_far", name, p.id)
        store.commit()
        if empty:
            progress(0, 0, f"Skipping {len(empty):,} of {len(posts):,} posts: no comments to download")
        skip = {p.id for p in empty}
        posts = [p for p in posts if p.id not in skip]
    n_comments = 0

    def save(p: Post, got) -> None:
        nonlocal n_comments
        comments, whole = got
        had = store.comment_ids(p.id)  # a thread fetched again: count only what's new
        for c in comments:
            store.save_comment(c, *score(c.body))
            n_comments += c.id not in had
        store.mark_comments(p.id, ok=True, settled=p.score is not None)
        if whole:  # an unsettled thread may still be growing: it has every comment so far, not all
            store.mark_complete("comments" if p.score is not None else "comments_so_far", name, p.id)
        store.commit()
        saved()

    errors, tried, gave_up = _each_thread(posts, client.fetch_all_comments, save, "All comments for post",
                                          store, getattr(client, "tree_workers", getattr(client, "workers", 1)),
                                          progress, should_stop)
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
    if posts:  # say what it's waiting on now, not only once the first thread is in
        progress(0, len(posts), f"{what} 0/{len(posts)}")
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
