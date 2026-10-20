"""Client for the Arctic Shift archive (https://arctic-shift.photon-reddit.com).

Arctic Shift is a free, community-run archive of Reddit posts and comments. It needs no account or
key. API reference: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md

What callers should know:
  * Posts can only be searched by date. The archive has no Reddit-style sorts, so "top" posts are
    found here by pulling a pool of posts from each part of the range and ranking them by score.
  * Items are archived within seconds of being posted, then fetched again later (usually about 36
    hours, sometimes a week). Until that second pass, ``score`` and ``num_comments`` are placeholders
    (usually 1), so they are reported as None here. ``_meta.retrieved_2nd_on`` marks the second pass.
  * The archive keeps content that was later removed on Reddit; removed and deleted items are
    skipped.
  * Every request goes through one RateLimiter, so a whole job (posts, refreshes and comments) stays
    at a few requests a second (BASE_INTERVAL) and slows down when the server sends a 429.
"""
from __future__ import annotations

import itertools
import math
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Collection, Iterable

import requests

from subsentiment import __version__
from subsentiment.reddit import (
    Comment, FetchError, Post, ProgressFn, StopFn, normalize_subreddit, subreddit_error,
)

BASE_URL = "https://arctic-shift.photon-reddit.com"
USER_AGENT = f"ikari/{__version__} (+https://github.com/nekomancer01/ikari)"
SETTLE_SECONDS = 36 * 3600
PAGE = 100  # the API's largest fixed page size
IDS_BATCH = 500  # most ids /api/posts/ids takes in one request
ATTEMPTS = 8
BASE_INTERVAL = 0.4  # seconds between requests: the API docs say "a few requests per second is fine"
MAX_WAIT = 120.0
MAX_INTERVAL = 10.0
SLOTS_PER_DAY = 4  # a day is split into at most this many slices, so posts spread across the day too
MIN_SLOTS = 60  # ...but a long range is split into at least this many (or one per day, if fewer)
TOP_SLOTS = 60  # top mode pulls a big pool per slice, so it uses fewer slices
TOP_PAGES = 4  # pages pulled per slice in top mode
# Top mode ranks a light copy of each post (no title or text, so the archive's query stays cheap), then
# fetches the winners in full by id. Removed posts only show up in the full copy, so a few spares are kept.
TOP_FIELDS = "id,created_utc,score,num_comments"
TOP_SPARE = 0.25
SLOT_PAGES = 10
WORKERS = 4  # requests in flight at once. The limiter still spaces out when each one starts; this only
             # overlaps the second or two each spends waiting on the archive
TIMEOUT_TRIES = 3  # query timeouts on one request before the caller is asked to narrow it
GARBLED_TRIES = 3  # answers that aren't the archive's JSON before a request gives up
MIN_WINDOW = 600  # seconds; a timing-out window is halved down to this
MAX_SKIPS = 2  # windows that still time out at MIN_WINDOW skipped in a row before the fetch gives up
TREE_LIMIT = 300  # comments asked for per thread; big trees are slow for the archive and trip its limits
TREE_MAX = 25000  # the most comments the tree endpoint returns in one request
UNKNOWN_THREAD = 1000  # what fetch_all_comments asks for when a post's comment count isn't known yet
COUNT_CHUNK_DAYS = 31  # days per aggregate request; a timing-out chunk is split down to one day
DAY = 86400

_GONE = ("[deleted]", "[removed]")

WaitFn = Callable[[float, str], None]


class Stopped(Exception):
    """Stop was pressed: raised by ArcticShiftClient.get instead of sending (or retrying) a request, and
    caught by the fetch loops, which return what they have."""


STOP_POLL = 0.2  # seconds between Stop checks during a long wait


class QueryTimeout(FetchError):
    """The archive gave up on a query as too slow, even with small pages. A narrower date window
    usually works."""


class _Flight:
    """A request the limiter let out, until its answer comes."""

    def __init__(self, should_stop: StopFn | None):
        self.should_stop = should_stop

    def abandoned(self) -> bool:
        """The job that sent it was stopped, so nobody is waiting for its answer."""
        return bool(self.should_stop and self.should_stop())


class RateLimiter:
    """Keeps requests at least ``interval`` seconds apart, and holds them back when the server asks.

    Arctic Shift's limits change with server load. It answers 429 with ``X-RateLimit-Reset`` (seconds
    until the limit resets) or ``X-RateLimit-Reset-At`` (a Unix time), and may send
    ``X-RateLimit-Remaining`` on any response. Each 429 doubles the spacing (up to MAX_INTERVAL);
    successful requests ease it back to the base interval.
    """

    def __init__(self, interval: float = BASE_INTERVAL, clock: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], None] = time.sleep, wall: Callable[[], float] = time.time):
        self.base = self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._next = 0.0  # earliest time the next request may go out
        self.on_wait: WaitFn | None = None  # told about long pauses, so the UI can say why it's idle
        self._lock = threading.Condition()  # one limiter can be shared by jobs on several threads
        # With several requests in flight, "X-RateLimit-Remaining: 2" on one answer says nothing about
        # the requests already sent. So the limiter counts what's in flight, keeps the remaining budget
        # minus those, and sends one request at a time whenever it doesn't know the budget (before the
        # first answer, and after a reset until the next answer says what the new window allows).
        # The requests still out, each as the Stop check of the job that sent it (or None).
        self._in_flight: list[_Flight] = []
        self._budget: float | None = None  # requests left in the server's current window, if known
        self._budget_until = 0.0  # when that window resets
        self._budget_known = False  # the server has sent X-RateLimit-Remaining at least once
        self._answered = False  # any answer has arrived yet

    def wait(self, should_stop: StopFn | None = None) -> _Flight:
        """Block until a request may go out, and count it as in flight until ``observe`` or ``done``
        (given what this returns). Raises Stopped, sending nothing, if ``should_stop`` turns true meanwhile."""
        announced = False
        with self._lock:
            while True:
                if should_stop and should_stop():
                    raise Stopped
                now = self._clock()
                if self._budget is not None and now >= self._budget_until:
                    self._budget = None  # the window reset; the next answer gives the new budget
                unknown = not self._answered or (self._budget_known and self._budget is None)
                # Requests whose job was stopped don't hold the next one back: Stop leaves them running
                # unanswered (up to a minute), and waiting on them stalled the next fetch or count.
                if unknown and any(not f.abandoned() for f in self._in_flight):
                    self._lock.wait(STOP_POLL if should_stop else 1.0)
                    continue
                if self._budget is not None and self._budget <= 0:
                    self.hold(self._budget_until - now, "Arctic Shift's request limit was reached")
                    self._budget = None
                # Sleeping with the lock held keeps starts ``interval`` apart by the clock even when a
                # sleep overshoots (Windows' timer is coarse); planning start times ahead doesn't.
                delay = self._next - now
                if delay > 0:
                    if delay >= 3 and self.on_wait and not announced:
                        self.on_wait(delay, self._reason)
                        announced = True
                    if should_stop and delay > STOP_POLL:
                        self._sleep(STOP_POLL)  # then look again: Stop may have been pressed
                        continue
                    self._sleep(delay)
                self._next = self._clock() + self.interval
                flight = _Flight(should_stop)
                self._in_flight.append(flight)
                if self._budget is not None:
                    self._budget -= 1
                return flight

    def done(self, flight: _Flight | None = None) -> None:
        """A request ``wait`` let out got no answer (a network error)."""
        with self._lock:
            self._answer(flight)
            self._lock.notify_all()

    def _answer(self, flight: _Flight | None) -> None:
        if flight in self._in_flight:
            self._in_flight.remove(flight)
        elif self._in_flight:
            self._in_flight.pop(0)

    def hold(self, seconds: float, reason: str = "Arctic Shift is busy") -> None:
        """Send nothing for the next ``seconds`` (capped at MAX_WAIT)."""
        until = self._clock() + min(max(seconds, 0.0), MAX_WAIT)
        if until > self._next:
            self._next = until
            self._reason = reason

    def slow_down(self) -> None:
        self.interval = min(self.interval * 2, MAX_INTERVAL)

    def ok(self) -> None:
        self.interval = max(self.base, self.interval * 0.8)

    def reset_after(self, headers, default: float) -> float:
        """Seconds until the server's limit resets, from whichever header it sent."""
        if (s := _number(headers.get("X-RateLimit-Reset"), None)) is not None:
            return s
        if (at := _number(headers.get("X-RateLimit-Reset-At"), None)) is not None:
            return at - self._wall()
        return default

    def observe(self, headers, flight: _Flight | None = None) -> None:
        """Note an answer's rate-limit headers, and pause until the reset when no requests are left."""
        with self._lock:
            self._answer(flight)
            self._answered = True
            remaining = _number(headers.get("X-RateLimit-Remaining"), None)
            if remaining is not None:
                reset = self.reset_after(headers, 10.0)
                self._budget_known = True
                self._budget = remaining - len(self._in_flight)
                self._budget_until = self._clock() + max(reset, 0.0)
                if remaining <= 0:
                    self.hold(reset, "Arctic Shift's request limit was reached")
            self._lock.notify_all()

    _reason = "Arctic Shift is busy"


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _meta(d: dict) -> dict:
    meta = d.get("_meta")
    return meta if isinstance(meta, dict) else {}


def _int(value) -> int:
    """A count from the archive as an int; 0 for anything that isn't a finite number ("1.5" is 1)."""
    n = _number(value, 0)
    return int(n) if math.isfinite(n) else 0


def _settled(d: dict) -> bool:
    """True once the archive has re-fetched the item, so score and comment count are real."""
    if _meta(d).get("retrieved_2nd_on"):
        return True
    return _number(d.get("retrieved_on"), 0) - _number(d.get("created_utc"), 0) >= SETTLE_SECONDS


def _removed(d: dict) -> bool:
    meta = _meta(d)
    return bool(d.get("removed_by_category") or meta.get("removal_type") or meta.get("was_deleted_later"))


def _post(d: dict) -> Post:
    settled = _settled(d)
    subreddit = _text(d.get("subreddit"))
    selftext = _text(d.get("selftext"))
    permalink = _text(d.get("permalink")) or f"/r/{subreddit}/comments/{d['id']}/"
    return Post(
        id=d["id"],
        subreddit=subreddit,
        title=_text(d.get("title")),
        selftext="" if selftext in _GONE else selftext,
        author=_text(d.get("author")) or "[deleted]",
        score=_int(d.get("score")) if settled else None,
        upvote_ratio=_ratio(d.get("upvote_ratio")) if settled else None,
        num_comments=_int(d.get("num_comments")) if settled else None,
        created_utc=_number(d.get("created_utc"), 0),
        permalink="https://www.reddit.com" + permalink,
        url=_text(d.get("url")),
    )


def _ratio(value) -> float:
    n = _number(value, 0.0)
    return n if math.isfinite(n) else 0.0


def _with_text_id(d: dict) -> dict:
    """``d`` with its id as text, as ids are compared with saved ones (a numeric one never matched)."""
    if d.get("id") is not None and not isinstance(d["id"], str):
        d = dict(d, id=str(d["id"]))
    return d


def _text(value) -> str:
    """A text field from the archive; "" for a missing one, or one that isn't text."""
    return value if isinstance(value, str) else ""


def slices(after: float, before: float, n: int) -> list[tuple[int, int]]:
    """[after, before) cut into n back-to-back windows of equal length."""
    step = (before - after) / n
    edges = [int(after + i * step) for i in range(n)] + [int(before)]
    return [(lo, hi) for lo, hi in zip(edges, edges[1:]) if hi > lo]


def quotas(total: int, n: int) -> list[int]:
    """``total`` split into n near-equal whole parts, the extras spread out rather than bunched."""
    return [(total * (i + 1)) // n - (total * i) // n for i in range(n)]


def slot_count(after: float, before: float, limit: int, top: bool) -> int:
    days = max(1, math.ceil((before - after) / 86400))
    if top:
        return max(1, min(limit, days, TOP_SLOTS))
    return max(1, min(limit, days * SLOTS_PER_DAY, max(days, MIN_SLOTS)))


def _light_rank(d: dict) -> tuple:
    """Sort key for top mode's light copies. Posts the archive hasn't re-checked still carry placeholder
    scores (usually 1), so they sink below real ones."""
    return (_number(d.get("score"), 0), _number(d.get("num_comments"), 0))


class ArcticShiftClient:
    def __init__(self, session: requests.Session | None = None, limiter: RateLimiter | None = None,
                 workers: int = WORKERS, should_stop: StopFn | None = None):
        self.workers = workers
        # Checked before every request and during waits, so Stop doesn't sit out retries and backoffs.
        self.should_stop = should_stop
        self._local = threading.local()  # per-thread state of get(), as several threads share a client
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.limiter = limiter or RateLimiter()
        self._page_cap = PAGE  # largest page the archive has answered without timing out in this fetch
        # Posts the archive returned that were removed on Reddit, id -> created_utc: fetching skips them,
        # but the archive's daily counts include them, so Download coverage counts them as checked.
        self.removed: dict[str, float] = {}
        self._removed_lock = threading.Lock()
        self.exhausted = False  # whether the last fetch_posts read its whole range (see there)
        self.timed_out = False  # whether it had to skip a stretch the archive kept timing out on
        self._skipped_posts = False  # a slice moved past posts it couldn't page through (see _earliest)

    # -- HTTP ---------------------------------------------------------------

    def get(self, path: str, params: dict, shrink: bool = True) -> list:
        """One API call with retries: waits on 429, backs off on 5xx and network errors, and on a
        query timeout backs off and asks for smaller pages (unless ``shrink`` is False, for requests
        whose ``limit`` isn't a page size), raising QueryTimeout if that doesn't help."""
        params = dict(params)
        self._local.last_params = params  # what was finally sent, after any shrinking
        timeouts = garbled = 0
        stop = None if getattr(self._local, "finishing", False) else self.should_stop
        for attempt in range(ATTEMPTS):
            backoff = min(5.0 * 2 ** attempt, MAX_WAIT)
            flight = self.limiter.wait(stop)
            try:
                resp = self.session.get(BASE_URL + path, params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError):  # no answer, or one cut off on the way
                self.limiter.done(flight)
                if attempt == ATTEMPTS - 1:
                    raise
                self.limiter.hold(backoff, "Couldn't reach Arctic Shift")
                continue
            except BaseException:
                self.limiter.done(flight)
                raise
            self.limiter.observe(resp.headers, flight)
            payload = _json(resp)  # parsed once: a big comment tree takes a while to parse
            error = _error_text(payload)
            if resp.status_code == 200 and not error and isinstance(payload, dict):
                self.limiter.ok()
                data = payload.get("data")
                # Anything that isn't an object is a malformed record; skipping it keeps the rest.
                return [_with_text_id(d) for d in data if isinstance(d, dict)] if isinstance(data, list) else []
            if resp.status_code == 200 and not isinstance(payload, dict):
                # Not the archive's JSON: a proxy's or CDN's error page, or a body cut short. It used to fail
                # the whole fetch on the first one; a few are retried, then it's said what came back.
                garbled += 1
                if garbled >= GARBLED_TRIES:
                    raise FetchError("Arctic Shift's answers couldn't be read (something between this computer "
                                     "and the archive may be answering instead). Try again in a few minutes.")
                self.limiter.hold(2.0 * garbled, "Arctic Shift's answer couldn't be read")
                continue
            if _is_timeout(resp.status_code, error):
                # The archive answers slow queries with "Query timed out" (HTTP 200) or "Timeout. Maybe
                # slow down a bit" (HTTP 422). Wait, then try a smaller page.
                timeouts += 1
                if timeouts >= TIMEOUT_TRIES:
                    raise QueryTimeout("Arctic Shift timed out on this query. Try a shorter date range or "
                                       "fewer posts, or try again in a few minutes.")
                # A timeout is about how heavy the query was, not how often we ask, so it gets a short
                # pause and a smaller page rather than slowing the whole job down.
                self.limiter.hold(2.0 * timeouts, "Arctic Shift is slow right now")
                limit = params.get("limit")
                if shrink and "limit" in params:
                    params["limit"] = PAGE // 4 if not isinstance(limit, int) else max(10, limit // 2)
                continue
            if resp.status_code == 429:
                self.limiter.slow_down()
                self.limiter.hold(self.limiter.reset_after(resp.headers, backoff),
                                  "Arctic Shift asked ikari to slow down")
                continue
            if resp.status_code >= 500:
                self.limiter.hold(backoff)
                continue
            if resp.status_code == 200:
                raise FetchError(f"Arctic Shift: {error}")
            raise FetchError(f"Arctic Shift returned HTTP {resp.status_code}" + (f": {error}" if error else "."))
        raise FetchError("Arctic Shift is busy or rate limiting. Try again in a few minutes.")

    @property
    def last_params(self) -> dict:
        """The parameters this thread's last get() finally sent, after any shrinking."""
        return getattr(self._local, "last_params", {})

    # -- posts ----------------------------------------------------------------

    def fetch_posts(
        self,
        subreddit: str,
        after: float | None = None,
        before: float | None = None,
        limit: int = 100,
        query: str = "",
        top: bool = False,
        skip: Collection[str] = (),
        progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
        on_posts: Callable[[list[Post]], None] | None = None,
        exhaust: bool = False,
    ) -> list[Post]:
        """Up to ``limit`` posts created in [after, before), spread evenly over the range, oldest first.

        The range is cut into equal slices (several per day for short ranges, about one a day for
        long ones) and each slice gets an equal share of ``limit``. Without ``top``, a slice's share
        is its earliest posts not in ``skip``, so a repeat fetch picks up posts it didn't have. With
        ``top``, each slice's share is the highest-scored posts from a pool pulled from across the
        slice; only settled posts have scores, so in a range that is all recent posts, nothing ranks.

        ``on_posts`` is given each batch of posts as soon as it is downloaded (each slice's, outside top
        mode, where posts can only be ranked at the end), so a caller can show them while the rest comes.

        Slices that run out leave others to make up the difference, a few rounds at most; with
        ``exhaust`` (not in top mode), rounds go on until every slice has been read to its end or
        ``limit`` is reached. Afterwards ``self.exhausted`` says whether every slice was read to its end,
        so the range holds no posts beyond the ones returned and those in ``skip``.

        A stretch the archive keeps timing out on, even narrowed to MIN_WINDOW, is skipped rather than
        failing the whole fetch; ``self.timed_out`` then says so, and the range isn't ``exhausted``.
        """
        subreddit = normalize_subreddit(subreddit)
        if error := subreddit_error(subreddit):
            raise FetchError(error)
        before = time.time() if before is None else before
        after = before - 7 * 86400 if after is None else after
        base: dict = {"subreddit": subreddit, "sort": "asc"}
        if query:
            base["query"] = query
        skip = set(skip)
        self._page_cap = PAGE
        self.exhausted = self.timed_out = self._skipped_posts = False

        n = slot_count(after, before, limit, top)
        windows = slices(after, before, n)
        picked: list[list[Post]] = [[] for _ in windows]
        spare: list[list] = [[] for _ in windows]  # top mode: ranked candidates not picked yet, best first
        more = [True] * len(windows)  # the slice may hold posts we haven't read
        cursors = [lo for lo, _ in windows]
        what = "Ranking" if top else "Collecting"

        def read_slice(job):
            i, want = job
            lo, hi = windows[i]
            if should_stop and should_stop():
                return None
            if top:
                ranked = sorted(self._top_pool(base, lo, hi, should_stop), key=_light_rank, reverse=True)
                return self._take_top(ranked, want)
            return self._earliest(base, lo, hi, want, skip, should_stop)

        # Slices are independent, so a few are read at once. Results are taken here, on the calling
        # thread, so on_posts can save them to a database that belongs to it.
        done = 0
        # Outside top mode, Stop returns at once rather than waiting out searches still in flight (up to a
        # minute for a slow one); what they'd bring is dropped, as nothing of it was shown or saved yet.
        # Top mode keeps a slice it has ranked (see _take_top), so it waits for that.
        for (i, want), result, error in in_parallel(read_slice, enumerate(quotas(limit, len(windows))),
                                                   self.workers, None if top else should_stop):
            if error:
                raise error
            done += 1
            if result is None:
                continue
            if top:
                picked[i], spare[i] = result
                more[i] = False
            else:
                picked[i], cursors[i], more[i] = result
            if on_posts and picked[i]:
                on_posts(picked[i])
            if progress:
                got = sum(map(len, picked))
                progress(f"{what} posts from r/{subreddit}: part {done} of {len(windows)}, {got:,} so far")

        # Quiet slices (night hours, a slow week) leave the total short. Share what's missing among the
        # slices that had more to give, a few rounds at most (as many as it takes, with exhaust).
        for _ in itertools.count() if exhaust and not top else range(3):
            short = limit - sum(map(len, picked))
            if top:
                open_ = [i for i, extra in enumerate(spare) if extra]
            else:
                open_ = [i for i, m in enumerate(more) if m]
            if short <= 0 or not open_ or (should_stop and should_stop()):
                break
            shares = [(i, want) for i, want in zip(open_, quotas(short, len(open_))) if want > 0]
            if top:
                for (i, _), (extra, rest), error in in_parallel(
                        lambda job: self._take_top(spare[job[0]], job[1]), shares, self.workers):
                    if error:
                        raise error
                    picked[i] += extra
                    spare[i] = rest
                    if on_posts and extra:
                        on_posts(extra)
                continue

            def read_more(job):
                i, want = job
                have = skip | {p.id for p in picked[i]}
                return self._earliest(base, cursors[i], windows[i][1], want, have, should_stop)

            for (i, _), result, error in in_parallel(read_more, shares, self.workers, should_stop):
                if error:
                    raise error
                extra, cursors[i], more[i] = result
                picked[i] += extra
                if on_posts and extra:
                    on_posts(extra)
        posts = [p for group in picked for p in group]
        posts.sort(key=lambda p: p.created_utc)
        self.exhausted = not top and not any(more) and not self._skipped_posts and not self.timed_out
        return posts

    def _page(self, base: dict, lo: int, hi: int, size) -> tuple[list[dict], int, int]:
        """Posts from [lo, hi), the end of the window actually searched, and the page size actually
        asked for: when the archive times out, the page is made smaller, then the window is halved
        (down to MIN_WINDOW) and only its first part is searched."""
        while True:
            try:
                page = self.get("/api/posts/search", dict(base, after=lo, before=hi, limit=size))
                return page, hi, self.last_params.get("limit", size)
            except QueryTimeout:
                if hi - lo <= MIN_WINDOW:
                    raise
                hi = lo + (hi - lo) // 2

    def _earliest(self, base, lo, hi, want, skip, should_stop) -> tuple[list[Post], int, bool]:
        """The first ``want`` usable posts in [lo, hi) that aren't in ``skip``, where to carry on
        reading from, and whether the slice may hold more."""
        out: list[Post] = []
        seen: set[str] = set()
        cursor = lo - 1  # `after` is exclusive; this way a post right on the slice's start isn't lost
        skipped = 0  # windows skipped in a row
        wide = bool(skip)
        for _ in range(SLOT_PAGES):
            # A few spare for removed posts; a full page when saved posts may need skipping. Once the
            # archive has timed out on a big page, this fetch keeps to the smaller size that worked.
            size = min(self._page_cap, PAGE if wide else want - len(out) + 5)
            try:
                page, end, sent = self._page(base, cursor, hi, size)
            except Stopped:
                return out, cursor, True
            except QueryTimeout:
                # Even the narrowest window: skip it and say the range wasn't all read. Not for a keyword
                # search, which times out whatever the window, nor when nothing near it answers either.
                skipped += 1
                if "query" in base or skipped > MAX_SKIPS:
                    raise
                self.timed_out = True
                cursor += MIN_WINDOW
                if cursor >= hi - 1:
                    return out, cursor, False
                continue
            skipped = 0
            if sent < size:
                self._page_cap = sent
            size = sent  # a page shrunk after a timeout is full at this size, not at the size asked for
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            fresh: list[Post] = []
            for d in new:
                seen.add(d["id"])
                if _removed(d):
                    self._note_removed(d)
                elif d["id"] not in skip and len(out) < want:
                    out.append(_post(d))
                    fresh.append(out[-1])
                    cursor = max(cursor, int(_number(d.get("created_utc"), 0)) - 1)
            if len(out) >= want or (should_stop and should_stop()):
                return out, cursor, True
            if end < hi and (len(page) < size or not new):  # only part of the window was searched
                cursor = end - 1
                continue
            if len(page) < size:
                return out, cursor, False
            if not new:
                # A full page of posts already seen: they all share the second the page starts on (a
                # burst of spam, usually removed), so asking again from there gets the same page. That
                # used to end the slice as if it were read to its end, dropping the rest of it.
                if size < self._page_cap:
                    wide = True  # a bigger page reaches past that second
                    continue
                # More than a page in one second (never seen): move past it rather than ask forever, and
                # don't call the range read to its end.
                cursor = int(max(_number(d.get("created_utc"), 0) for d in page))
                self._skipped_posts = True
                continue
            # The next page starts a second before this one ended, so posts sharing that timestamp
            # aren't lost; `seen` drops the repeats.
            cursor = int(max(_number(d.get("created_utc"), 0) for d in page)) - 1
        return out, cursor, True

    def _top_pool(self, base, lo, hi, should_stop) -> list[dict]:
        """Light candidates (TOP_FIELDS only) for the top posts of [lo, hi): a page from each of
        TOP_PAGES equal parts of the slice, so a busy slice isn't judged on its first few hours only."""
        pool: dict[str, dict] = {}
        for start, end in slices(lo, hi, TOP_PAGES):
            if should_stop and should_stop():
                break
            try:
                # `after` is exclusive, so start - 1: a post made right on the slice's start is a candidate.
                page = self._page(dict(base, fields=TOP_FIELDS), start - 1, end, PAGE)[0]
            except Stopped:
                break
            for d in page:
                if d.get("id"):
                    pool[d["id"]] = d
        return list(pool.values())

    def _take_top(self, ranked: list, want: int) -> tuple[list[Post], list]:
        """The best ``want`` of a slice's ranked candidates as full posts, and the rest, best first.

        Candidates are light copies (or full posts left over from an earlier call), so the picks plus a
        few spares are downloaded by id in one request; picks that turn out to be removed are replaced by
        the next best. Done per slice, so a slice's posts show as soon as it is ranked. Stop isn't
        checked here: a slice that was ranked is kept, rather than thrown away for want of one request."""
        head = ranked[:want + math.ceil(want * TOP_SPARE)]
        light = [d["id"] for d in head if isinstance(d, dict)]
        self._local.finishing = True  # Stop can't cut this request short (see above)
        try:
            full = {p.id: p for p in self.fetch_posts_by_ids(light)} if light else {}
        finally:
            self._local.finishing = False
        usable = [d if isinstance(d, Post) else full[d["id"]] for d in head
                  if isinstance(d, Post) or d["id"] in full]
        return usable[:want], usable[want:] + ranked[len(head):]

    def fetch_posts_by_ids(self, ids: Iterable[str], should_stop: StopFn | None = None) -> list[Post]:
        """Current copies of the given posts, IDS_BATCH per request; removed ones are left out."""
        ids = list(dict.fromkeys(ids))
        out: list[Post] = []
        for i in range(0, len(ids), IDS_BATCH):
            if should_stop and should_stop():
                break
            try:
                page = self.get("/api/posts/ids", {"ids": ",".join(ids[i:i + IDS_BATCH])})
            except Stopped:
                break
            for d in page:
                if not d.get("id"):
                    continue
                if _removed(d):
                    self._note_removed(d)
                else:
                    out.append(_post(d))
        return out

    def _note_removed(self, d: dict) -> None:
        created = _number(d.get("created_utc"), None)
        if created is not None:
            with self._removed_lock:  # slice threads Stop left running may still be adding
                self.removed[d["id"]] = float(created)

    def take_removed(self) -> dict[str, float]:
        """The removed posts noted so far, which are then forgotten."""
        with self._removed_lock:
            taken, self.removed = self.removed, {}
        return taken

    def day_counts(self, subreddit: str, after: float, before: float,
                   should_stop: StopFn | None = None) -> dict[int, int]:
        """Posts the archive holds for each UTC day in [after, before), as {day start: count}, from its
        aggregate endpoint. The counts include posts later removed on Reddit, which fetching skips. Days
        the archive couldn't count (it times out on very busy subreddits) are left out, not given 0.

        The archive keys each day's bucket at its own midnight, two hours ahead of UTC (22:00Z the day
        before, while Europe is on summer time), though the bucket holds the UTC day: on r/AskReddit the
        bucket keyed 2026-10-16T22:00Z held 4,288 posts, exactly the 24 hourly buckets of UTC 17 Oct. So
        a bucket counts for the UTC day whose midnight is nearest its key."""
        subreddit = normalize_subreddit(subreddit)
        if error := subreddit_error(subreddit):
            raise FetchError(error)
        first, end = int(after // DAY * DAY), int(math.ceil(before / DAY) * DAY)
        todo = [(lo, min(lo + COUNT_CHUNK_DAYS * DAY, end)) for lo in range(first, end, COUNT_CHUNK_DAYS * DAY)]
        out: dict[int, int] = {}
        failed: list[int] = []
        retried = False
        while todo or (failed and not retried):
            if should_stop and should_stop():
                break
            if not todo:  # one more go at the days that timed out, now the archive has had a breather
                todo, failed, retried = [(d, d + DAY) for d in failed], [], True
            lo, hi = todo.pop(0)
            params = {"subreddit": subreddit, "aggregate": "created_utc", "frequency": "day",
                      # Unlike the search, the aggregate lays its buckets out from `after`'s own second, so
                      # `after` must be the day start (checked live: start - 1 times out, start - 2 gives
                      # buckets keyed 21:59:58Z holding 0 posts).
                      "after": lo, "before": hi, "limit": (hi - lo) // DAY + 1}
            try:
                buckets = self.get("/api/posts/search/aggregate", params, shrink=False)
            except Stopped:
                break
            except QueryTimeout:
                if hi - lo > DAY:  # try the halves
                    mid = lo + (hi - lo) // DAY // 2 * DAY
                    todo[:0] = [(lo, mid), (mid, hi)]
                else:
                    failed.append(lo)
                continue
            for day in range(lo, hi, DAY):
                out[day] = 0  # a day with no bucket had no posts
            for b in buckets:
                when, n = _bucket_time(b), _number((b or {}).get("count", (b or {}).get("doc_count")), None)
                if when is None or n is None:
                    continue
                day = int((when + DAY // 2) // DAY * DAY)
                if lo <= day < hi:
                    out[day] += int(n)
        return out

    # -- comments ---------------------------------------------------------------

    def fetch_comments(self, post: Post, limit: int = 100) -> list[Comment]:
        """The top ``limit`` comments on ``post`` by score, highest first (unscored ones last).

        Uses the archive's comment tree, which returns a whole thread in one request but can't sort it,
        so the thread is ranked here. A thread with more than TREE_LIMIT comments is cut short by the
        archive and ranked from what came back. Comments the archive hasn't re-checked yet have no
        score, so on a post from the last day or two the "top" ones are only a sample.
        """
        # Ask for the whole thread when its size is known, so the top ones are really the top ones;
        # half the cap while it isn't (the post's numbers haven't settled).
        known = post.num_comments + 20 if post.num_comments is not None else TREE_LIMIT // 2
        size = min(TREE_LIMIT, max(limit, PAGE, known))
        comments = self._thread(post, size)[0]
        comments.sort(key=lambda c: -1 if c.score is None else c.score, reverse=True)
        return comments[:limit]

    def fetch_all_comments(self, post: Post) -> tuple[list[Comment], bool]:
        """Every comment the archive has on ``post`` (up to TREE_MAX), and whether that is the whole
        thread: False when the archive cut it short or left replies collapsed."""
        known = post.num_comments if post.num_comments is not None else UNKNOWN_THREAD
        size = min(TREE_MAX, max(PAGE, known + 50))
        comments, items, collapsed = self._thread(post, size)
        sent = _number(self.last_params.get("limit"), size)  # smaller if the archive timed out
        return comments, items < sent and not collapsed

    def _thread(self, post: Post, size: int) -> tuple[list[Comment], int, bool]:
        """The usable comments of a post's tree (parents first), how many items came back, and whether
        any replies were left collapsed."""
        tree = self.get("/api/comments/tree", {"link_id": post.id, "limit": size})
        comments: list[Comment] = []
        items = 0
        for d in _walk(tree):
            items += 1
            body = _text(d.get("body"))
            if not d.get("id") or body in _GONE or _removed(d):
                continue
            permalink = _text(d.get("permalink")) or f"/r/{post.subreddit}/comments/{post.id}/_/{d['id']}/"
            comments.append(Comment(
                id=str(d["id"]),
                post_id=post.id,
                subreddit=post.subreddit,
                parent_id=_text(d.get("parent_id")),
                author=_text(d.get("author")) or "[deleted]",
                body=body,
                score=_int(d.get("score")) if _settled(d) else None,
                created_utc=_number(d.get("created_utc"), 0),
                depth=int(d["depth"]) if isinstance(d.get("depth"), int) else 0,
                permalink="https://www.reddit.com" + permalink,
            ))
        return comments, items, _collapsed(tree)


def in_parallel(fn: Callable, items: Iterable, workers: int = WORKERS, should_stop: StopFn | None = None):
    """Run fn(item) for each item, up to ``workers`` at once, and yield (item, result, error) as each
    finishes, on the calling thread. Stopping early (closing the generator) starts nothing more.

    With ``should_stop``, once it turns true this returns without waiting for calls still running: their
    results are dropped. For calls that can take long and are cheap to redo, like a big comment tree.
    The worker threads are daemons, so one left on a slow request never holds up the app quitting."""
    items = list(items)
    if workers <= 1 or len(items) <= 1:
        for item in items:
            try:
                yield item, fn(item), None
            except Exception as e:  # noqa: BLE001 - handed to the caller, like the threaded path
                yield item, None, e
        return
    todo, done = queue.SimpleQueue(), queue.SimpleQueue()
    for item in items:
        todo.put(item)
    closed = threading.Event()

    def work():
        while not closed.is_set():
            try:
                item = todo.get_nowait()
            except queue.Empty:
                return
            try:
                done.put((item, fn(item), None))
            except Exception as e:  # noqa: BLE001 - handed to the caller
                done.put((item, None, e))

    threads = [threading.Thread(target=work, daemon=True) for _ in range(min(workers, len(items)))]
    for t in threads:
        t.start()
    try:
        for _ in items:
            while True:
                try:
                    result = done.get(timeout=STOP_POLL if should_stop else None)
                    break
                except queue.Empty:
                    if should_stop():
                        return
            yield result
            if should_stop and should_stop():
                return
    finally:
        closed.set()  # nothing new starts
        if not (should_stop and should_stop()):
            for t in threads:
                t.join()


def _walk(items: list, depth: int = 0):
    """Comments in a Reddit-style tree ({"kind": "t1", "data": {..., "replies": Listing}}), parents
    before replies. "more" stubs (collapsed replies the archive didn't expand) are skipped."""
    for item in items or ():
        if not isinstance(item, dict) or item.get("kind") != "t1":
            continue
        data = item.get("data")
        d = dict(data) if isinstance(data, dict) else {}
        if not isinstance(d.get("depth"), int):  # missing, or null in a malformed record
            d["depth"] = depth
        yield d
        replies = d.get("replies")
        if isinstance(replies, dict):
            yield from _walk((replies.get("data") or {}).get("children"), d["depth"] + 1)


def _collapsed(items: list) -> bool:
    """True if a comment tree holds any "more" stubs (replies the archive didn't expand)."""
    for item in items or ():
        if not isinstance(item, dict):
            continue
        if item.get("kind") == "more" and ((item.get("data") or {}).get("children") or
                                           (item.get("data") or {}).get("count")):
            return True
        replies = (item.get("data") or {}).get("replies")
        if isinstance(replies, dict) and _collapsed((replies.get("data") or {}).get("children")):
            return True
    return False


def _bucket_time(b) -> float | None:
    """A date bucket's start in epoch seconds. The archive may give seconds, milliseconds or an ISO date."""
    if not isinstance(b, dict):
        return None
    v = b.get("created_utc", b.get("key", b.get("date")))
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.replace(".", "", 1).isdigit()):
        v = float(v)
        return v / 1000 if v > 1e11 else v
    if isinstance(v, str):
        try:
            when = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).timestamp()
    return None


def _is_timeout(status: int, error: str) -> bool:
    text = error.lower()
    return "timed out" in text or "timeout" in text or (status == 422 and "slow down" in text)


def _json(resp):
    """The response's JSON, or None when it isn't JSON."""
    try:
        return resp.json()
    except Exception:
        return None


def _error_text(payload) -> str:
    return str(payload.get("error") or "") if isinstance(payload, dict) else ""
