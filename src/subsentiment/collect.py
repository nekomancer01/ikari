"""One fetch job: pull posts (and optionally comments), score them, store them."""
from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Collection

import numpy as np
import requests

from subsentiment.arctic_shift import ArcticShiftClient, ArchiveDown, Stopped, in_parallel, keep
from subsentiment.reddit import FetchError, Post, normalize_subreddit
from subsentiment.sentiment import Lexicon
from subsentiment.store import Store

GIVE_UP_AFTER = 5  # consecutive posts whose comments couldn't be fetched before the rest are skipped
DAY = 86400
MAX_DAY_POSTS = 10000  # cap on one UTC day when downloading everything (busy subreddits post ~4,500 a day)
# Days in a row the archive timed out on without giving a single post before a download stops reading days:
# it's struggling with every query, and each such day costs dozens of requests that come to nothing.
TIMED_OUT_DAYS = 2
# Days in a row whose threads kept failing (GIVE_UP_AFTER in a row) before no later day's comments are asked for:
# the archive can't give comments just now, and each day would only add more failing requests.
GAVE_UP_DAYS = 2
# Downloading every post, days in a row are read together, as one range, when the days read so far say they hold
# about BATCH_POSTS posts between them (a page for each slice: four requests, as one day takes), up to BATCH_DAYS:
# a quiet subreddit's month took four searches a day, whatever the day held, and a comment round of its own each.
BATCH_POSTS = 400
BATCH_DAYS = 31


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
    # Which comments a cap keeps: "top" (highest-scored) or "random" (fixed per post). SAMPLE ignores the cap: each
    # UTC day keeps a random sample of its comments big enough to measure its sentiment (see Sample).
    comment_pick: str = "top"
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


SAMPLE = "sample"  # FetchRequest.comment_pick for a statistical sample of each day's comments


class Sample:
    """Which comments of one UTC day's saved posts make a simple random sample of the day's comments, big enough
    that each share (positive, negative, neutral) it measures is within MARGIN of the whole day's 19 times in 20.
    The day's comments are its posts' own counts (num_comments); the sample needs NEED of a big day, fewer of a
    smaller one (the finite population correction), and a day with no more than NEED is kept whole.

    The archive gives a thread whole or not at all (its tree can't sample or sort), so only threads holding part
    of the sample are fetched, and each keeps its quota, picked at random by keep. A quota comes from one draw per
    comment the post counts, from a generator seeded by the post's id, the day keeping its ``size`` lowest draws:
    every comment of the day is as likely to be in as any other, so a busy thread gets its share and no more,
    and the same posts give the same sample each time it's asked for."""

    Z, MARGIN = 1.96, 0.05  # 95% confidence, ±5 points
    EXACT = Z * Z * 0.25 / (MARGIN * MARGIN)  # a share of 50% is the hardest to pin down: 384.2 comments
    NEED = math.ceil(EXACT)

    def __init__(self, counts: dict[str, int | None]):
        """``counts``: each of the day's saved posts (not image ones) with its num_comments, None if not counted yet."""
        known = {pid: n for pid, n in counts.items() if n}
        self.counts = counts
        self.population = sum(known.values())
        self.whole = self.population <= self.NEED
        self.size = self.population if self.whole else math.ceil(
            self.EXACT / (1 + (self.EXACT - 1) / self.population))
        self.quotas: dict[str, int] = {}
        if not self.whole:
            draws = {pid: np.random.default_rng(int.from_bytes(
                hashlib.blake2b(pid.encode(), digest_size=8).digest(), "little")).random(n) for pid, n in known.items()}
            line = np.partition(np.concatenate(list(draws.values())), self.size - 1)[self.size - 1]
            self.quotas = {pid: int(np.count_nonzero(d <= line)) for pid, d in draws.items()}

    def quota(self, pid: str) -> int | None:
        """How many of a post's comments the sample keeps; None for its whole thread: the day is small, or the post's
        comments aren't counted yet (about its first 36 hours), so its share of the day isn't known."""
        n = self.counts.get(pid)
        if n is None or self.whole:
            return None
        return min(self.quotas.get(pid, 0), n)

    def keep(self, pid: str, comments: list) -> list:
        """The sample's share of a thread as it came back: its quota, scaled to the tree's real size, since the
        archive's count lags it (1,078 counted, 1,264 in the tree, 22 Oct), so each comment keeps the same chance."""
        q, n = self.quota(pid), self.counts.get(pid)
        if q is None:
            return comments
        return keep(comments, min(len(comments), max(1, round(q * len(comments) / n))), "random", pid) if q else []


def run(client: ArcticShiftClient, store: Store, req: FetchRequest,
        progress: Callable[[int, int, str], None] = lambda done, total, msg: None,
        should_stop: Callable[[], bool] = lambda: False,
        saved: Callable[[], None] = lambda: None,
        score_all: Callable[[Lexicon, list[str]], list[tuple[float, str]] | None] = lambda lexicon, texts: [
            lexicon.score(t) for t in texts]) -> FetchResult:
    """Run one fetch. ``saved`` is called after each batch of rows is committed, so a UI can show
    them while the fetch goes on. ``score_all`` scores a thread's comments by the subreddit's words (here, unless
    it's given another way)."""
    return Download(client, store, req, progress, should_stop, saved, score_all).run()


class Download:
    """One fetch job (see run): what it was asked for, what's saved already, and what it has done so far."""

    def __init__(self, client: ArcticShiftClient, store: Store, req: FetchRequest, progress, should_stop, saved,
                 score_all):
        self.client, self.store, self.req = client, store, req
        self.shown, self.should_stop, self.saved, self.score_all = progress, should_stop, saved, score_all
        self.said = (0, 0, "")
        self.name = normalize_subreddit(req.subreddit)
        if (limiter := getattr(client, "limiter", None)) is not None:
            limiter.on_wait = self.waiting
        if hasattr(client, "should_stop"):
            client.should_stop = should_stop  # so Stop also cuts short retries and waits inside a request
        self.stored: dict = {}  # saved posts of the range, id -> row
        self.streamed: set[str] = set()  # new posts saved as they were downloaded
        self.rechecked: set[str] = set()  # saved posts whose scores were re-fetched first
        self.sample = req.comment_pick == SAMPLE
        self.cap = 0 if self.sample else req.comments_per_post  # a sample asks for each thread whole, then cuts it
        self.fill = req.fill_threads or not self.cap  # keep asking for a thread until it's whole, or has cap saved
        self.samples: dict[int, Sample] = {}  # each UTC day's sample, planned once its posts are saved
        self.kept: dict[str, int] = {}  # threads read for the sample: how many comments each kept
        self.saved_whole: set[str] = set()  # saved posts whose whole thread is saved (when filling threads)
        self.saved_count: dict[str, int] = {}  # comments saved per post (when filling threads up to a cap)
        self.n_comments = self.errors = 0
        self.gave_up = 0  # parts (days) in a row whose threads kept failing
        self.timed_out = False  # some part of the range was skipped, as the archive kept timing out on it

    def progress(self, done: int, total: int, msg: str) -> None:  # remembered, to come back to after a wait
        self.said = (done, total, msg)
        self.shown(done, total, msg)

    def note(self, msg: str) -> None:
        self.progress(0, 0, msg)

    def waiting(self, secs: float, why: str) -> None:
        # Once the pause is over, what the fetch is doing comes back: the wait's line used to stay up until
        # the next progress, a whole slice of a busy day later, so the fetch read as stuck waiting.
        if secs:
            self.shown(0, 0, f"{why}. Waiting {secs:.0f} s before the next request…")
        elif self.said[2]:
            self.shown(*self.said)

    def score(self, text: str):
        return self.store.lexicon(self.name).score(text)  # by the subreddit's own words, as set now

    def run(self) -> FetchResult:
        try:
            return self.comment_posts() if self.req.comment_posts else self.posts()
        finally:
            limiter = getattr(self.client, "limiter", None)
            if limiter is not None and getattr(limiter, "on_wait", None) == self.waiting:
                limiter.on_wait = None  # not left holding this job, and everything it read, once it's over

    def posts(self) -> FetchResult:
        """The posts of the range, and their comments (see run)."""
        req, store, name = self.req, self.store, self.name
        # Asked for, whatever comes of it: Download coverage lists these days, to find what this fetch misses.
        end = time.time() if req.before is None else req.before
        store.save_tried(name, req.days or days_in(end - 7 * DAY if req.after is None else req.after, end))
        picked_days = set(req.days)
        self.stored = {r["id"]: r for r in store.posts_between(name, req.after, req.before)
                       if not picked_days or (r["created_utc"] or 0) // DAY * DAY in picked_days}  # not days between

        # 1. If asked, saved posts from this range whose scores weren't settled yet: re-fetch them in batches.
        stale = [pid for pid, r in self.stored.items() if r["score"] is None and not r["image"]] \
            if req.refresh_scores else []
        updated: list[Post] = []
        if stale:
            self.note(f"Checking {len(stale):,} saved posts for updated scores…")
            for p in self.client.fetch_posts_by_ids(stale, should_stop=self.should_stop):
                if p.score is not None:
                    store.save_post(p, *self.score(p.text))
                    updated.append(p)
            store.commit()
            self.saved()
        self.rechecked = {p.id for p in updated}

        # 2. New posts, then 3. their comments (see comments).
        if self.fill and req.include_comments:
            self.saved_whole = store.complete_keys("comments", name)
            if self.cap:
                self.saved_count = store.comment_counts(name, req.after, req.before)
        if req.limit is None:
            picked, new, settled = self.by_day(updated)
        else:
            picked = self.read(req.after, req.before, req.limit, req.everything, self.note)
            new, settled = self.settle(picked)
            self.comments(picked, updated + settled, lambda pid: True, self.progress)
        updated += settled
        first = (new or updated or picked or [None])[0]
        subreddit = first.subreddit if first else name  # canonical capitalisation from Reddit
        return FetchResult(subreddit, len(new), self.n_comments, self.should_stop(), updated=len(updated),
                           already=len(picked) - len(new) - len(settled), comment_errors=self.errors,
                           timed_out=self.timed_out)

    def by_day(self, updated: list[Post]) -> tuple[list[Post], list[Post], list[Post]]:
        """Every post of the range, newest first, a run of days at a time (see batch): each run's posts, then their
        comments, so a long fetch fills in as it goes and Stop keeps whole runs. Returns (picked, new, settled) as
        run uses them."""
        req, progress = self.req, self.progress
        picked: list[Post] = []
        new: list[Post] = []
        settled: list[Post] = []
        days = [(d, d + DAY) for d in req.days] if req.days else _days(self.name, self.store, req.after, req.before)
        seen: set[str] = set()  # saved posts on the days read, whose comments those days took care of
        fruitless = 0  # runs in a row the archive timed out on without giving a post
        rate = None  # posts a second on the days read last, saved ones too; None: not known, or it timed out
        k = n = 0
        while k < len(days):
            if self.should_stop() or fruitless >= TIMED_OUT_DAYS:
                break
            n = self.batch(days, k, rate, 2 * n)
            lo, hi = days[k + n - 1][0], days[k][1]
            when = " to ".join(dict.fromkeys(time.strftime("%a %d %b", time.gmtime(t)) for t in (lo, hi - 1)))
            which = f"Day {k + 1}" if n == 1 else f"Days {k + 1}-{k + n}"

            def say(_msg, k=k, which=which, when=when):
                # Short, as the window shows one line: the bar counts days, this says which and how many.
                progress(k, len(days), f"{which} of {len(days)}, {when}: {len(self.streamed):,} new posts")

            def report(done, total, _msg, k=k, which=which, when=when):
                progress(k, len(days), f"{which} of {len(days)}, {when}: comments on {done:,} of {total:,} posts")
            say("")
            in_run = {pid for pid, r in self.stored.items() if lo <= (r["created_utc"] or 0) < hi}
            seen |= in_run
            got = self.read(lo, hi, MAX_DAY_POSTS * n, True, say, skip=in_run)
            timed_out = getattr(self.client, "timed_out", False)
            fruitless = fruitless + 1 if timed_out and not got else 0
            rate = None if timed_out or self.should_stop() else (len(got) + len(in_run)) / (hi - lo)
            day_new, day_settled = self.settle(got)
            picked += got
            new += day_new
            settled += day_settled
            day_updated = [p for p in updated if lo <= p.created_utc < hi] + day_settled
            self.comments(got, day_updated, in_run.__contains__, report)
            k += n
        # Days already downloaded in full aren't read again, but their saved posts can still be owed comments.
        self.comments([], [p for p in updated if p.id not in seen], lambda pid: pid not in seen, progress)
        if days:
            progress(len(days), len(days), f"{len(self.streamed):,} new posts from r/{self.name}")
        return picked, new, settled

    @staticmethod
    def batch(days: list[tuple[float, float]], k: int, rate: float | None, most: int) -> int:
        """How many of ``days`` (newest first) from the k-th to read together: the ones right before it, one after
        another, that ``rate`` (posts a second, from the days read last) says hold about BATCH_POSTS between them,
        and no more than ``most`` (twice the last run): today, or a day the archive has nothing for yet, can read
        as quiet on a busy subreddit, so runs grow a step at a time. One while the rate isn't known: the first
        day, and after a stretch the archive timed out on."""
        n = 1
        while (rate is not None and n < min(most, BATCH_DAYS) and k + n < len(days)
               and days[k + n][1] == days[k + n - 1][0]
               and rate * (days[k][1] - days[k + n][0]) <= BATCH_POSTS):
            n += 1
        return n

    def save_streamed(self, batch: list[Post]) -> None:
        """New posts, saved and announced the moment they're downloaded, so a UI can show them right away. One
        commit for the batch: a commit each, hundreds after Stop (what slices still running had), took over 30 s
        on a Windows runner, each one written through to disk."""
        new = False
        for p in batch:
            if p.id not in self.stored and p.id not in self.streamed:
                self.store.save_post(p, *self.score(p.text))
                self.streamed.add(p.id)
                new = True
        if new:
            self.store.commit()
            self.saved()

    def read(self, after, before, limit: int, everything: bool, say,
             skip: Collection[str] | None = None) -> list[Post]:
        """Posts of [after, before) up to ``limit``. An even sample skips everything already saved, so a repeat
        fetch adds new posts; top mode ranks everything, then keeps the ones that aren't saved yet."""
        req, store, name, client = self.req, self.store, self.name, self.client
        skip = self.stored.keys() if skip is None else skip
        # Where earlier fetches got to, so a stopped download goes on from there. Not for a day that's complete,
        # where Check again looks through all of it for posts the archive has taken in since.
        whole = _whole_days(after, before)
        again = bool(whole and {str(d) for d in whole} & store.complete_keys("posts", name))
        got = client.fetch_posts(name, after, before, limit, req.query, top=req.top,
                                 skip=() if req.top else skip,
                                 progress=say, should_stop=self.should_stop, on_posts=self.save_streamed,
                                 exhaust=everything, read=() if again else store.read_spans(name))
        self.timed_out |= bool(getattr(client, "timed_out", False))  # each fetch_posts says only for itself
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
        # Each whole UTC day of the range is complete only when every part of it was read to its end (no stretch
        # the client left unread is on it, as days read together can differ): a fetch that stopped at the limit,
        # or was stopped, may have missed posts. A day that isn't over yet can still get posts, so it never is,
        # and a keyword search only reads the posts that match.
        if everything and not req.query and not self.should_stop():
            unread = [] if getattr(client, "exhausted", False) else getattr(client, "unread", None) or [(after, before)]
            for day in whole:
                if day + DAY <= time.time() and not any(lo < day + DAY and hi > day for lo, hi in unread):
                    store.mark_complete("posts", name, str(day))
        return got

    def settle(self, picked: list[Post]) -> tuple[list[Post], list[Post]]:
        """Saves what a part's read picked that the stream didn't, and returns (new, settled): top mode can
        pick a saved post whose score has come in since, and that settled copy is kept (it used to be dropped,
        leaving the score blank unless "Update saved posts" was ticked)."""
        stored, store = self.stored, self.store
        new = [p for p in picked if p.id not in stored]
        for p in new:
            if p.id not in self.streamed:
                store.save_post(p, *self.score(p.text))
        settled = [p for p in picked if p.id in stored and p.id not in self.rechecked
                   and stored[p.id]["score"] is None and p.score is not None]
        for p in settled:
            store.save_post(p, *self.score(p.text))
        store.commit()
        self.saved()
        return new, settled

    def comments(self, picked: list[Post], updated: list[Post], in_part: Callable[[str], bool], report) -> None:
        """Comments for one part of the fetch: for its new posts, for saved ones whose scores just settled
        (their comment scores have too), and for saved posts whose comments were never fetched or failed last
        time. A post whose comments came back fine, even if there were none, isn't asked about again. Asked for
        all comments, every saved post in the range is, until its whole thread is saved (as Download all
        comments does); with fill_threads and a cap, until it has that many saved. ``in_part`` says whether a
        saved post's id belongs to this part."""
        req, store, stored, client = self.req, self.store, self.stored, self.client
        if not req.include_comments:
            return
        cap, fill = self.cap, self.fill
        refreshed = {p.id for p in updated}
        repicked = {p.id for p in picked if p.id in stored}  # top mode can pick posts that are already saved
        todo = [p for p in picked if p.id not in stored] + updated + [
            _stored_post(r) for pid, r in stored.items()
            if in_part(pid) and not r["image"] and pid not in refreshed and (
                # Asked for before and failed. Saved posts nobody asked comments for are left alone: a fetch must
                # not quietly download comments for posts it wasn't fetching.
                (r["comments_ok"] is not None and not r["comments_ok"])
                or (pid in repicked and r["comments_ok"] is None and not r["has_comments"])  # picked, never asked
                or (fill and pid not in self.saved_whole and not (cap and self.saved_count.get(pid, 0) >= cap)))]
        todo = [p for p in todo if p.num_comments != 0]  # None (not settled yet) still gets looked up
        stopped = self.should_stop()  # before any was asked about: left owed, below
        if not stopped:
            todo = self.sampled(todo)
        # Threads failing one after another stop this part's (its day's): the rest are left for the next fetch. The
        # next day tries again, so one bad stretch of a year's download doesn't leave every day after it without;
        # after GAVE_UP_DAYS such days, no more are tried.
        tried: set[str] = set()
        gave_up = self.gave_up >= GAVE_UP_DAYS
        if not gave_up and not stopped:
            get = self.take_sample if self.sample else (lambda p: client.fetch_all_comments(p, cap, req.comment_pick)) \
                if fill else (lambda p: (client.fetch_comments(p, cap, req.comment_pick), False))
            failed, tried, gave_up, added = self.each_thread(todo, get, cap=cap, progress=report)
            self.errors += failed
            self.n_comments += added
            self.mark_sampled(tried)
            self.gave_up = self.gave_up + 1 if gave_up else 0
        if gave_up:
            rest = [p for p in todo if p.id not in tried]  # not tried this time: retried on the next fetch
            self.errors += len(rest)
            for p in rest:
                store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
        elif self.should_stop():
            # Stopped before these were asked about: owed, so the next fetch asks. It may not read their day again
            # (one read to its end is complete), and a post never asked about is otherwise left alone.
            for p in todo:
                if p.id not in tried and (p.id not in stored or stored[p.id]["comments_ok"] is None):
                    store.mark_comments(p.id, ok=False, settled=False)
            store.commit()
        if todo and not stopped:
            report(len(todo), len(todo), "Done")

    def comment_posts(self) -> FetchResult:
        """Every comment on each post in req.comment_posts. Posts whose numbers hadn't settled are
        re-checked first, so their comment totals are real where the archive has them by now."""
        req, store, name, progress = self.req, self.store, self.name, self.progress
        posts = [_stored_post(r) for r in store.posts_by_ids(req.comment_posts)]
        stale = [p.id for p in posts if p.score is None]
        fresh: dict[str, Post] = {}
        if stale:
            progress(0, 0, f"Checking {len(stale):,} saved posts for updated comment counts…")
            fresh = {p.id: p for p in self.client.fetch_posts_by_ids(stale, should_stop=self.should_stop)
                     if p.score is not None}
            for p in fresh.values():
                store.save_post(p, *self.score(p.text))
            store.commit()
            posts = [fresh.get(p.id, p) for p in posts]
        cap = self.cap if req.fill_threads else 0  # Download coverage's cap; 0: every comment
        if cap:  # a thread with its cap saved already is done for this cap
            posts = [p for p in posts if len(store.comment_ids(p.id)) < cap]
        posts = self.sampled(posts)
        if len(posts) > 1:
            # A day's Download all comments offers posts whose comments weren't counted yet; once re-checked, the
            # ones with none, or with every one saved already, cost no request. They're done, so they aren't
            # offered again. One post asked about on its own (its Check again) is asked whatever its count says:
            # a count can lag.
            empty = [p for p in posts
                     if p.num_comments is not None and len(store.comment_ids(p.id)) >= p.num_comments]
            for p in empty:
                store.mark_comments(p.id, ok=True, settled=p.score is not None)
                store.mark_complete("comments" if p.score is not None else "comments_so_far", name, p.id)
            store.commit()
            if empty:
                progress(0, 0, f"Skipping {len(empty):,} of {len(posts):,} posts: no comments to download")
            skip = {p.id for p in empty}
            posts = [p for p in posts if p.id not in skip]
        errors, tried, gave_up, n_comments = self.each_thread(
            posts, self.take_sample if self.sample else lambda p: self.client.fetch_all_comments(p, cap, req.comment_pick),
            cap=cap, progress=progress, every=True)
        self.mark_sampled(tried)
        if gave_up:
            errors += len([p for p in posts if p.id not in tried])  # not tried: retried next time
        if posts:
            progress(len(posts), len(posts), "Done")
        subreddit = posts[0].subreddit if posts else name
        return FetchResult(subreddit, 0, n_comments, self.should_stop(), updated=len(fresh), comment_errors=errors)

    def sampled(self, posts: list[Post]) -> list[Post]:
        """Of these saved posts, the ones whose threads the sample still wants comments from (every one, when not
        sampling). The rest, with no part in their day's sample or holding it already, are marked done for the
        sample ("sampled"), as Download coverage counts them."""
        if not self.sample:
            return posts
        store, name, todo = self.store, self.name, []
        held = store.complete_keys("sampled", name)
        for p in posts:
            day = int(p.created_utc // DAY * DAY)
            if day not in self.samples:
                self.samples[day] = Sample(store.comment_totals(name, day, day + DAY))
                self.saved_count.update(store.comment_counts(name, day, day + DAY))
            q = self.samples[day].quota(p.id)
            if q is None or (p.id not in held and self.saved_count.get(p.id, 0) < q):
                todo.append(p)
            elif p.id not in held:
                store.mark_complete("sampled", name, p.id)
        store.commit()
        return todo

    def take_sample(self, p: Post) -> tuple[list, bool]:
        """each_thread's get when sampling: the whole thread, cut to the post's part of its day's sample."""
        comments, whole = self.client.fetch_all_comments(p)
        sample = self.samples[int(p.created_utc // DAY * DAY)]
        kept = sample.keep(p.id, comments)
        if sample.quota(p.id) is not None and comments:  # a tree that came back empty is asked again next time
            self.kept[p.id] = len(kept)
        return kept, whole and len(kept) == len(comments)

    def mark_sampled(self, tried: set[str]) -> None:
        """Threads read for the sample and saved (``tried``, not ones dropped after Stop) whose part of it is saved
        now: done for the sample."""
        for pid in tried & self.kept.keys():
            if len(self.store.comment_ids(pid)) >= self.kept[pid]:
                self.store.mark_complete("sampled", self.name, pid)
        self.kept.clear()
        self.store.commit()

    def each_thread(self, posts: list[Post], get: Callable, *, cap: int, progress,
                    every: bool = False) -> tuple[int, set[str], bool, int]:
        """Run get(post), which returns (comments, whether they're the whole thread), for each post, a few at
        once, and save each thread here, on this thread, scored by the subreddit's words (``self.score_all``);
        ``self.saved`` is told once it's committed, if it brought comments (``every``: whatever it brought).
        ``cap`` is the comments asked for per thread, 0 for all. A post whose comments couldn't be fetched is
        recorded as failed, so the next fetch retries it; after GIVE_UP_AFTER failures in a row no new ones
        start, since the archive is struggling and shouldn't be hammered (by a whole range: see comments). Returns (posts that failed, ids of the
        posts tried, whether it gave up, new comments)."""
        store, name, client, should_stop = self.store, self.name, self.client, self.should_stop
        what = "Sampled comments for post" if self.sample else "Comments for post" if cap else "All comments for post"
        workers = getattr(client, "tree_workers", getattr(client, "workers", 1))
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
        errors = added = 0
        if posts:  # say what it's waiting on now, not only once the first thread is in
            progress(0, len(posts), f"{what} 0/{len(posts)}")
        for p, got, error in in_parallel(fetch, posts, workers, should_stop):
            if got is None and error is None:
                continue
            tried.add(p.id)
            progress(len(tried), len(posts), f"{what} {len(tried)}/{len(posts)}: {p.title[:60]}")
            if error is not None:
                # Down for good (it waited OUTAGE_PATIENCE): the job ends, rather than each thread left waiting it
                # out.
                if isinstance(error, ArchiveDown) or not isinstance(error, (FetchError, requests.RequestException)):
                    raise error
                store.mark_comments(p.id, ok=False, settled=False)
                store.commit()
                errors += 1
                continue
            comments, whole = got
            # by the subreddit's words as they are set now
            if (scores := self.score_all(store.lexicon(name), [c.body for c in comments])) is None:
                tried.discard(p.id)  # stopped while scoring: left as if never asked (owed, as comments says)
                continue
            added += store.save_comments(comments, scores)  # a thread asked for again: only what's new counts
            store.mark_comments(p.id, ok=True, settled=p.score is not None)
            if whole:  # an unsettled thread may still be growing, so it's whole only "so far"
                store.mark_complete("comments" if p.score is not None else "comments_so_far", name, p.id)
            store.commit()
            if comments or every:
                self.saved()
        return errors, tried, gave_up.is_set(), added


def days_in(after: float, before: float) -> list[int]:
    """UTC day starts overlapping [after, before), newest first."""
    first = int(after // DAY * DAY)
    last = int(math.ceil(before / DAY) * DAY) - DAY
    return list(range(last, first - 1, -DAY))


def _whole_days(after: float | None, before: float | None) -> list[int]:
    """Starts of the UTC days wholly inside [after, before)."""
    if after is None or before is None:
        return []
    return list(range(int(math.ceil(after / DAY) * DAY), int(before) - DAY + 1, DAY))


def _days(name: str, store: Store, after: float | None, before: float | None) -> list[tuple[float, float]]:
    """[after, before) as UTC days, newest first, each clipped to the range; past days already downloaded
    in full are left out, as they hold nothing new."""
    before = time.time() if before is None else before
    after = before - 7 * DAY if after is None else after
    done = store.complete_keys("posts", name)
    return [(max(day, after), min(day + DAY, before)) for day in days_in(after, before)
            if str(day) not in done and min(day + DAY, before) > max(day, after)]


def _stored_post(r) -> Post:
    return Post(r["id"], r["subreddit"], r["title"] or "", r["selftext"] or "", r["author"] or "", r["score"],
                r["upvote_ratio"], r["num_comments"], r["created_utc"] or 0.0, r["permalink"] or "", r["url"] or "")
