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

import hashlib
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
    Comment, FetchError, Post, ProgressFn, StopFn, is_image_post, normalize_subreddit, subreddit_error,
)

BASE_URL = "https://arctic-shift.photon-reddit.com"
USER_AGENT = f"ikari/{__version__} (+https://github.com/nekomancer01/ikari)"
SETTLE_SECONDS = 36 * 3600
PAGE = 100  # the API's largest fixed page size
IDS_BATCH = 500  # most ids /api/posts/ids takes in one request
LIMITED_TRIES = 8  # 429s in a row on one request before it gives up
RETRIES = 5  # tries of a request that keeps failing (5xx, no answer) while the archive answers others
# Requests failed in a row, with nothing answered between, for the archive to count as down: as many as a request's own
# tries, so a lone request (a count) finds an outage before it gives up, and a share failing at random rarely looks like one.
OUTAGE_AFTER = RETRIES
PROBE_WAITS = (10.0, 20.0, 40.0, 60.0)  # seconds between checks while it's down: then every 60 s
OUTAGE_PATIENCE = 600.0  # seconds a request waits for a down archive before its job ends (ArchiveDown)
# Asked while it's down, rather than a request that may be failing for its own reasons: a lookup of one old post.
PROBE = ("/api/posts/ids", {"ids": "ei30r4", "fields": "id"})
TIMEOUT = (10, 60)  # seconds to connect (an archive that isn't there is noticed quickly), and between bytes read
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
MAX_SKIPS = 2  # windows that still time out at MIN_WINDOW skipped in a row before the slice is left unread
# A comment thread is one cheap lookup by post id; the archive answers it in 1-2.5 s and has never timed one out or
# limited it (live, 21 Oct: 173 threads on r/AskReddit and r/Deltarune, 4 in flight and 0.4 s apart, no 422 or 429).
# Spacing and in-flight count were what held "Download all comments" to about two posts a second, so threads go
# out twice as often and twice as many at once. A 429 still doubles the spacing for everything.
TREE_SHARE = 0.5
TREE_WORKERS = 8
TREE_LIMIT = 300  # comments asked for per thread; big trees are slow for the archive and trip its limits
TREE_MAX = 25000  # the most comments the tree endpoint returns in one request
INGEST_LAG = 3600  # seconds; a stretch read more recently than this may still get posts (see fetch_posts)
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
    """A request the limiter let out, until its answer comes. ``probe``: the archive is down, and this thread is to
    ask it the cheapest question (PROBE) instead of its own. ``since``: when the request was first asked for, which
    its later tries pass back to RateLimiter.wait."""

    def __init__(self, should_stop: StopFn | None, since: float, probe: bool = False):
        self.should_stop = should_stop
        self.since = since
        self.probe = probe

    def abandoned(self) -> bool:
        """The job that sent it was stopped, so nobody is waiting for its answer."""
        return bool(self.should_stop and self.should_stop())


class ArchiveDown(FetchError):
    """The archive hasn't answered anything for OUTAGE_PATIENCE: the job ends, keeping what it saved."""


class RateLimiter:
    """The one door every request to the archive goes through. It keeps requests at least ``interval`` seconds
    apart, within the server's budget, and holds them all back only on the archive's own word about load or when
    it isn't answering at all; a request that fails on its own waits on its own thread (ArcticShiftClient.get).

    Arctic Shift's limits change with server load. It answers 429 with ``X-RateLimit-Reset`` (seconds
    until the limit resets) or ``X-RateLimit-Reset-At`` (a Unix time), and may send
    ``X-RateLimit-Remaining`` on any response. Each 429 doubles the spacing (up to MAX_INTERVAL);
    successful requests ease it back to the base interval.

    Down: after OUTAGE_AFTER failures in a row (5xx, or no answer) with nothing answered between, nothing more
    goes out but one PROBE at a time, PROBE_WAITS apart; the first answer, to a probe or to a request already out,
    ends it. Once it has been down OUTAGE_PATIENCE, a request waiting raises ArchiveDown as soon as a probe sent
    since it began has failed: so every job ends together, and one started later asks once first. (Live, 22
    Oct 03:14 UTC: every request, even the smallest, answered HTTP 500 for over ten minutes; each request used to
    hold everything back 5 s, 10 s, ... 120 s, eight times, before failing.)

    Waits are slept with the lock let go, so answers coming in and other jobs' Stop aren't held up by them.
    """

    def __init__(self, interval: float = BASE_INTERVAL, clock: Callable[[], float] = time.perf_counter,
                 sleep: Callable[[float], None] = time.sleep, wall: Callable[[], float] = time.time):
        self.base = self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._next = 0.0  # earliest time the next request may go out
        # Told about long pauses, so the UI can say why it's idle, and told (0, "") when one is over.
        self.on_wait: WaitFn | None = None
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
        self._failures = 0  # requests failed in a row, across every job
        self._answered_at = float("-inf")  # when the last request was answered
        self._down: tuple[float, str, float] | None = None  # since it's down: (wall time, what came back, clock time)
        self._probes = 0  # probes that failed since
        self._probe_at = 0.0  # when the next may go
        self._probe_failed = float("-inf")  # when the last one failed
        self._probing = False  # one is out

    @property
    def down(self) -> bool:
        return self._down is not None

    def down_text(self) -> str:
        since, what, _ = self._down or (self._wall(), "", 0.0)
        return f"Arctic Shift isn't answering ({what} since {time.strftime('%H:%M', time.localtime(since))})"

    def wait(self, should_stop: StopFn | None = None, share: float = 1.0, since: float | None = None) -> _Flight:
        """Block until a request may go out, and count it as in flight until ``observe`` or ``done``
        (given what this returns). Raises Stopped, sending nothing, if ``should_stop`` turns true meanwhile.
        ``share`` is how much of the spacing the request takes up: less for a light one (see TREE_SHARE).
        While the archive is down, the flight returned may be a probe (see the class), and ArchiveDown is raised
        once it has been down too long (see the class): ``since`` is when this request was first asked for."""
        announced = False
        with self._lock:
            since = self._clock() if since is None else since
            while True:
                if should_stop and should_stop():
                    raise Stopped
                now = self._clock()
                if self._down is not None:
                    long = now - self._down[2] >= OUTAGE_PATIENCE
                    if long and self._probe_failed >= since:
                        raise ArchiveDown(f"{self.down_text()}. What was downloaded is kept; try again once "
                                          "it's back.")
                    due = max(self._next, now if long and self._probe_failed < since else self._probe_at)
                    if not self._probing and now >= due:
                        self._probing = True
                        return _Flight(should_stop, since, probe=True)
                    if not self._probing and self.on_wait and self._announced != due:  # once for each probe
                        self._announced = due
                        self.on_wait(due - now, self.down_text())
                    self._rest(STOP_POLL if should_stop else 1.0)
                    continue
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
                delay = self._next - now
                if delay > 0:
                    if delay >= 3 and self.on_wait and not announced:
                        self.on_wait(delay, self._reason)
                        announced = True
                    chunk, slot = (min(delay, STOP_POLL) if should_stop else delay), self._next
                    self._rest(chunk)
                    if should_stop and should_stop():  # pressed during that last short wait
                        raise Stopped
                    # Look again if it was only part of the wait (Stop may be pressed next), or another request
                    # took the slot or the archive went down meanwhile. Otherwise it's this one's, whatever the
                    # clock says: a coarse timer (Windows') can wake a little early.
                    if chunk < delay or self._next != slot or self._down is not None:
                        continue
                self._next = self._clock() + self.interval * share
                if announced and self.on_wait:
                    self.on_wait(0.0, "")  # over: the UI goes back to what the job is doing
                flight = _Flight(should_stop, since)
                self._in_flight.append(flight)
                if self._budget is not None:
                    self._budget -= 1
                return flight

    def _rest(self, seconds: float) -> None:
        """Sleep with the lock let go (it's held on the way in and out)."""
        self._lock.release()
        try:
            self._sleep(seconds)
        finally:
            self._lock.acquire()

    def done(self, flight: _Flight | None = None) -> None:
        """A request ``wait`` let out got no answer (a network error)."""
        with self._lock:
            self._answer(flight)
            self._lock.notify_all()

    def _answer(self, flight: _Flight | None) -> None:
        if flight in self._in_flight:
            self._in_flight.remove(flight)
        elif self._in_flight and (flight is None or not flight.probe):
            self._in_flight.pop(0)

    def pause(self, seconds: float, should_stop: StopFn | None = None) -> None:
        """Wait ``seconds`` on this thread only, letting other requests go meanwhile. Raises Stopped if
        ``should_stop`` turns true."""
        end = self._clock() + min(seconds, MAX_WAIT)
        while (left := end - self._clock()) > 0:
            if should_stop and should_stop():
                raise Stopped
            self._sleep(min(left, STOP_POLL) if should_stop else left)
        if should_stop and should_stop():
            raise Stopped

    def hold(self, seconds: float, reason: str = "Arctic Shift is busy") -> None:
        """Send nothing for the next ``seconds`` (capped at MAX_WAIT)."""
        until = self._clock() + min(max(seconds, 0.0), MAX_WAIT)
        if until > self._next:
            self._next = until
            self._reason = reason

    def slow_down(self) -> None:
        self.interval = min(self.interval * 2, MAX_INTERVAL)

    def ok(self, ease: bool = True) -> None:
        """A request was answered: the archive is up, and (if ``ease``: it was answered in full) the spacing eases
        back."""
        with self._lock:
            if ease:
                self.interval = max(self.base, self.interval * 0.8)
            self._failures = 0
            self._answered_at = self._clock()
            back = self._down is not None
            if back:
                self._down, self._probing = None, False
                self._lock.notify_all()
        if back and self.on_wait:
            self.on_wait(0.0, "")  # the UI goes back to what the job is doing

    def failed(self, what: str, since: float | None = None) -> bool:
        """A request failed (``what``: "HTTP 500", "no answer"); whether the archive now counts as down. With ``since``
        (when the request failed the time before), it's about to give up: unless something was answered since, the
        trouble may well be the archive's, so it counts as down now (a probe tells which). (Not since its first
        failure: answers to requests sent before the archive went down still come in just after.)"""
        with self._lock:
            self._failures += 1
            if since is not None and self._answered_at < since:
                self._failures = max(self._failures, OUTAGE_AFTER)
            if self._down is None and self._failures >= OUTAGE_AFTER:
                self._down, self._probes, self._probing = (self._wall(), what, self._clock()), 0, False
                self._probe_at = self._clock() + PROBE_WAITS[0]
            return self._down is not None

    def probed(self, up: bool) -> None:
        """The probe ``wait`` handed out came back: answered (``up``), or not, so the next waits longer."""
        if up:
            self.ok(ease=False)
            return
        with self._lock:
            self._probing = False
            if self._down is not None:
                self._probes += 1
                self._probe_failed = self._clock()
                self._probe_at = self._probe_failed + PROBE_WAITS[min(self._probes, len(PROBE_WAITS) - 1)]
            self._lock.notify_all()

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
    _announced = None  # when the probe last announced is due


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _meta(d: dict) -> dict:
    meta = d.get("_meta")
    return meta if isinstance(meta, dict) else {}


def _finite(value, kind=float):
    """A number from the archive as ``kind`` (int for a count: "1.5" is 1); 0 for anything that isn't a finite number."""
    n = _number(value, 0.0)
    return kind(n) if math.isfinite(n) else kind(0)


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
    selftext = "" if selftext in _GONE else selftext
    return Post(
        id=d["id"],
        subreddit=subreddit,
        title=_text(d.get("title")),
        selftext=selftext,
        author=_text(d.get("author")) or "[deleted]",
        score=_finite(d.get("score"), int) if settled else None,
        upvote_ratio=_finite(d.get("upvote_ratio")) if settled else None,
        num_comments=_finite(d.get("num_comments"), int) if settled else None,
        created_utc=_number(d.get("created_utc"), 0),
        permalink="https://www.reddit.com" + permalink,
        url=_text(d.get("url")),
        image=is_image_post(d.get("url"), selftext, d.get("post_hint") or "", d.get("is_gallery")),
    )


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


def slot_count(after: float, before: float, limit: int, top: bool, exhaust: bool = False) -> int:
    days = max(1, math.ceil((before - after) / 86400))
    if exhaust and not top:
        # Every post is read, so slices only share the work among the workers rather than spread a sample: a run of
        # quiet days is read in as many requests as one day, not four for each of its days.
        return max(1, min(limit, SLOTS_PER_DAY))
    if top:
        return max(1, min(limit, days, TOP_SLOTS))
    return max(1, min(limit, days * SLOTS_PER_DAY, max(days, MIN_SLOTS)))


class ArcticShiftClient:
    def __init__(self, session: requests.Session | None = None, limiter: RateLimiter | None = None,
                 workers: int = WORKERS, should_stop: StopFn | None = None):
        self.workers = workers
        self.tree_workers = max(workers, TREE_WORKERS)  # comment threads fetched at once (see TREE_SHARE)
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
        self.images: dict[str, Post] = {}
        self._removed_lock = threading.Lock()
        self.exhausted = False  # whether the last fetch_posts read its whole range (see there)
        self.timed_out = False  # whether it had to skip a stretch the archive kept timing out on
        # Stretches the last fetch_posts read through, as (first, last) seconds: every post created in them
        # was returned, skipped as saved, or noted as removed or an image (see there).
        self.reached: list[tuple[int, int]] = []
        # Stretches it didn't read to their end, as [first, end) seconds: windows skipped as the archive kept timing
        # out on them, seconds holding more posts than a page, and what's left of slices that stopped short. A day
        # of the range clear of them was read in full, though the range as a whole wasn't (see exhausted).
        self.unread: list[tuple[int, int]] = []

    # -- HTTP ---------------------------------------------------------------

    def get(self, path: str, params: dict, shrink: bool = True, share: float = 1.0) -> list:
        """One API call, with what each kind of failure calls for (see RateLimiter for the archive being down):

        * 429, or the budget spent: every request waits for the reset (RateLimiter.hold).
        * A query timeout: this request alone pauses 2 s, then 4 s, asking for smaller pages (unless ``shrink`` is
          False, for requests whose ``limit`` isn't a page size), then raises QueryTimeout for the caller to ask
          for less.
        * A 5xx, or no answer: this request alone tries again, 1, 2, 4 and 8 s later, RETRIES times in all, then
          raises (the network error itself, if that's what it was). Failures in a row across every request mean the
          archive is down: this request then waits for it to come back rather than using up its tries, and raises
          ArchiveDown if it doesn't within OUTAGE_PATIENCE.
        * An answer that isn't the archive's JSON: this request alone tries again, GARBLED_TRIES times in all.
        * Any other error is raised at once: the request is wrong, and asking again won't help.

        ``share`` is RateLimiter.wait's."""
        params = dict(params)
        self._local.last_params = params  # what was finally sent, after any shrinking
        timeouts = tries = limited = garbled = 0
        since = failed_at = None
        stop = None if getattr(self._local, "finishing", False) else self.should_stop
        while True:
            flight = self.limiter.wait(stop, share, since)
            since = flight.since
            if flight.probe:
                up = False
                try:
                    up = self._probe(flight)
                finally:  # whatever happened, the next probe can go
                    self.limiter.probed(up)
                continue
            try:
                resp = self.session.get(BASE_URL + path, params=params, timeout=TIMEOUT)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.ContentDecodingError) as e:  # no answer, or one cut off on the way
                self.limiter.done(flight)
                failure, what = e, "no answer"
            except BaseException:
                self.limiter.done(flight)
                raise
            else:
                self.limiter.observe(resp.headers, flight)
                try:
                    payload = resp.json()  # parsed once: a big comment tree takes a while to parse
                except Exception:  # not JSON
                    payload = None
                error = str(payload.get("error") or "") if isinstance(payload, dict) else ""
                if resp.status_code == 200 and not error and isinstance(payload, dict):
                    self.limiter.ok()
                    data = payload.get("data")
                    records = []
                    for d in data if isinstance(data, list) else ():
                        if not isinstance(d, dict):  # a malformed record: skipping it keeps the rest
                            continue
                        if d.get("id") is not None and not isinstance(d["id"], str):  # compared with saved ids as text
                            d = dict(d, id=str(d["id"]))
                        records.append(d)
                    return records
                lowered = error.lower()
                if "timed out" in lowered or "timeout" in lowered or (resp.status_code == 422 and "slow down" in lowered):
                    # The archive answers slow queries with "Query timed out" (HTTP 200) or "Timeout. Maybe
                    # slow down a bit" (HTTP 422). It's up, and the query was too heavy: wait, then try a smaller
                    # page. Only this request waits: holding every request back made one heavy query (a busy
                    # month's count) stall the downloads beside it, which then said "Arctic Shift is slow" over
                    # and over and saved nothing.
                    self.limiter.ok(ease=False)
                    timeouts += 1
                    if timeouts >= TIMEOUT_TRIES:
                        raise QueryTimeout("Arctic Shift timed out on this query. Try a shorter date range or "
                                           "fewer posts, or try again in a few minutes.")
                    self.limiter.pause(2.0 * timeouts, stop)
                    limit = params.get("limit")
                    if shrink and "limit" in params:
                        params["limit"] = PAGE // 4 if not isinstance(limit, int) else max(10, limit // 2)
                    continue
                if resp.status_code == 429:
                    self.limiter.ok(ease=False)  # answering, if not to this
                    self.limiter.slow_down()
                    self.limiter.hold(self.limiter.reset_after(resp.headers, 5.0 * 2 ** min(limited, 4)),
                                      "Arctic Shift asked ikari to slow down")
                    limited += 1
                    if limited >= LIMITED_TRIES:
                        raise FetchError("Arctic Shift is busy or rate limiting. Try again in a few minutes.")
                    continue
                if resp.status_code == 200 and isinstance(payload, dict):
                    self.limiter.ok(ease=False)
                    raise FetchError(f"Arctic Shift: {error}")
                if resp.status_code == 200:
                    # Not the archive's JSON: a proxy's or CDN's error page, or a body cut short. A few are retried,
                    # then it's said what came back; it isn't the archive answering, so waiting for that won't help.
                    garbled += 1
                    if garbled >= GARBLED_TRIES:
                        raise FetchError("Arctic Shift's answers couldn't be read (something between this computer "
                                         "and the archive may be answering instead). Try again in a few minutes.")
                    self.limiter.pause(2.0 * garbled, stop)
                    continue
                if resp.status_code < 500:  # the request is wrong: asking again won't help
                    self.limiter.ok(ease=False)
                    raise FetchError(f"Arctic Shift returned HTTP {resp.status_code}" + (f": {error}" if error else "."))
                failure, what = FetchError(f"Arctic Shift couldn't answer (HTTP {resp.status_code}). Try again in a "
                                           "few minutes."), f"HTTP {resp.status_code}"
            # A 5xx, or no answer: this request's own trouble, or the archive's.
            last, failed_at = failed_at, self.limiter._clock()
            if self.limiter.failed(what, last if tries + 1 >= RETRIES else None):
                continue  # waited for at the limiter, which probes until it's back
            tries += 1
            if tries >= RETRIES:  # while the archive answered others: it's this request's trouble
                raise failure
            self.limiter.pause(2.0 ** (tries - 1), stop)

    def _probe(self, flight) -> bool:
        """Ask the archive the cheapest question there is, for the limiter, which has it down: whether it answered."""
        path, params = PROBE
        try:
            resp = self.session.get(BASE_URL + path, params=params, timeout=(10, 20))
        except requests.RequestException:
            return False
        self.limiter.observe(resp.headers, flight)
        try:
            payload = resp.json()
        except Exception:
            return False
        if resp.status_code == 429:
            self.limiter.hold(self.limiter.reset_after(resp.headers, 10.0), "Arctic Shift asked ikari to slow down")
        return resp.status_code < 500 and isinstance(payload, dict)

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
        read: Iterable[tuple[int, int]] = (),
    ) -> list[Post]:
        """Up to ``limit`` posts created in [after, before), spread evenly over the range, oldest first.

        The range is cut into equal slices (several per day for short ranges, about one a day for
        long ones, SLOTS_PER_DAY with ``exhaust``) and each slice gets an equal share of ``limit``. Without
        ``top``, a slice's share is its earliest posts not in ``skip``, so a repeat fetch picks up posts it
        didn't have. With ``top``, each slice's share is the highest-scored posts from a pool pulled from across
        the slice; only settled posts have scores, so in a range that is all recent posts, nothing ranks.

        ``on_posts`` is given each batch of posts as soon as it is downloaded (each slice's, outside top
        mode, where posts can only be ranked at the end), so a caller can show them while the rest comes.

        Slices that run out leave others to make up the difference, a few rounds at most; with
        ``exhaust`` (not in top mode), rounds go on until every slice has been read to its end or
        ``limit`` is reached. Afterwards ``self.exhausted`` says whether every slice was read to its end,
        so the range holds no posts beyond the ones returned and those in ``skip``.

        A stretch the archive keeps timing out on, even narrowed to MIN_WINDOW, is skipped rather than
        failing the whole fetch; ``self.timed_out`` then says so, and the range isn't ``exhausted``.
        ``self.unread`` says which stretches weren't read to their end, so a day clear of them was.

        ``read`` is the stretches an earlier fetch read through (``self.reached`` after it, as (first, last)
        seconds), whose posts are all in ``skip``: each slice starts after what was read of it, so a download
        that was stopped picks up where it left off rather than paging through every post it already has.
        Afterwards ``self.reached`` says what this one read through (nothing for a keyword search or top mode).
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
        self.exhausted = self.timed_out = False
        self.reached, self.unread = [], []

        n = slot_count(after, before, limit, top, exhaust)
        windows = slices(after, before, n)
        picked: list[list[Post]] = [[] for _ in windows]
        spare: list[list] = [[] for _ in windows]  # top mode: ranked candidates not picked yet, best first
        # Where each slice's reading goes on from: the first second whose posts may not all be read yet.
        cursors = [lo for lo, _ in windows]
        if not (top or query):
            read = sorted(read)
            for i, (lo, _) in enumerate(windows):
                for first, last in read:
                    if first <= cursors[i] <= last:
                        cursors[i] = last + 1
        starts = list(cursors)
        more = [c < hi for c, (_, hi) in zip(cursors, windows)]  # the slice may hold posts we haven't read
        what = "Ranking" if top else "Collecting"
        # What each slice still running has downloaded so far, kept when Stop stops waiting for it.
        partial: dict[int, list[Post]] = {}

        def read_slice(job):
            i, want = job
            lo, hi = windows[i]
            if should_stop and should_stop():
                return None
            if top:
                # Posts the archive hasn't re-checked still carry placeholder scores (usually 1): they sink below
                # real ones.
                ranked = sorted(self._top_pool(base, lo, hi, should_stop), reverse=True,
                                key=lambda d: (_number(d.get("score"), 0), _number(d.get("num_comments"), 0)))
                return self._take_top(ranked, want)
            if not more[i]:  # read to its end before
                return [], hi, False
            return self._earliest(base, cursors[i], hi, want, skip, should_stop, partial.setdefault(i, []))

        # Slices are independent, so a few are read at once. Results are taken here, on the calling
        # thread, so on_posts can save them to a database that belongs to it.
        done = 0
        # Outside top mode, Stop returns at once rather than waiting out searches still in flight (up to a
        # minute for a slow one); the posts those slices had already downloaded are kept (see the end).
        # Top mode keeps a slice it has ranked (see _take_top), so it waits for that.
        for (i, want), result, error in in_parallel(read_slice, enumerate(quotas(limit, len(windows))),
                                                   self.workers, None if top else should_stop):
            if error:
                raise error
            done += 1
            partial.pop(i, None)
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
                return self._earliest(base, cursors[i], windows[i][1], want, have, should_stop,
                                      partial.setdefault(i, []))

            for (i, _), result, error in in_parallel(read_more, shares, self.workers, should_stop):
                if error:
                    raise error
                partial.pop(i, None)
                extra, cursors[i], more[i] = result
                picked[i] += extra
                if on_posts and extra:
                    on_posts(extra)
        # Slices Stop left running: what they had is used, and they may hold more.
        for i, extra in list(partial.items()):
            extra = list(extra)  # the slice's thread may still be adding to it
            picked[i] += extra
            if on_posts and extra:
                on_posts(extra)
            # Read in order, so it's read through the second before its newest post (more may share that one).
            cursors[i] = max([cursors[i]] + [int(p.created_utc) for p in extra])
        if not (top or query or self.unread):
            # Not past a while ago: the archive takes in new posts within seconds, but not always.
            now = int(time.time()) - INGEST_LAG
            self.reached = [(start, min(hi - 1 if not m else c - 1, now))
                            for start, c, m, (_, hi) in zip(starts, cursors, more, windows)]
            self.reached = [(a, b) for a, b in self.reached if b >= a]
        posts = [p for group in picked for p in group]
        posts.sort(key=lambda p: p.created_utc)
        if not top:
            self.unread += [(c, hi) for c, m, (_, hi) in zip(cursors, more, windows) if m]
        self.exhausted = not top and not any(more) and not self.unread
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

    def _earliest(self, base, lo, hi, want, skip, should_stop, out: list[Post] | None = None
                  ) -> tuple[list[Post], int, bool]:
        """The first ``want`` usable posts in [lo, hi) that aren't in ``skip``, where to carry on
        reading from, and whether the slice may hold more. Posts are added to ``out`` as they come, so a
        caller that stops waiting still has the ones already downloaded."""
        out = [] if out is None else out
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
                # search, which times out whatever the window. When nothing near it answers either, the rest
                # of the slice is left unread rather than failing the whole fetch: other slices and days may
                # still answer, and a range that timed out isn't called read to its end, so it's asked again.
                if "query" in base:
                    raise
                skipped += 1
                self.timed_out = True
                self.unread.append((cursor + 1, min(cursor + 1 + MIN_WINDOW, hi)))
                cursor += MIN_WINDOW
                if cursor >= hi - 1 or skipped > MAX_SKIPS:
                    if cursor < hi - 1:
                        self.unread.append((cursor + 1, hi))
                    return out, cursor, False
                continue
            skipped = 0
            if sent < size:
                self._page_cap = sent
            size = sent  # a page shrunk after a timeout is full at this size, not at the size asked for
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            for d in new:
                seen.add(d["id"])
                if (post := self._usable(d, skip)) is not None and len(out) < want:
                    out.append(post)
                    cursor = max(cursor, int(_number(d.get("created_utc"), 0)) - 1)
            if len(out) >= want or (should_stop and should_stop()):
                return out, cursor, True
            if end < hi and (len(page) < size or not new):  # only part of the window was searched
                if len(page) >= size and not new:  # a full page of repeats: more may share its second
                    self.unread.append((cursor + 1, end + 1))
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
                self.unread.append((cursor, cursor + 1))
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
            out += [post for d in page if d.get("id") and (post := self._usable(d)) is not None]
        return out

    def _usable(self, d: dict, skip: Collection[str] = ()) -> Post | None:
        """The post ``d`` is, if it's one to keep and not in ``skip``. A removed one is noted in ``removed`` (the
        archive's daily counts include it) and an image post not in ``skip`` in ``images`` (kept as a hidden row),
        under a lock: slice threads Stop left running may still be adding."""
        if _removed(d):
            created = _number(d.get("created_utc"), None)
            if created is not None:
                with self._removed_lock:
                    self.removed[d["id"]] = float(created)
            return None
        if d["id"] in skip:
            return None
        post = _post(d)
        if post.image:
            with self._removed_lock:
                self.images[post.id] = post
            return None
        return post

    def take_images(self) -> list[Post]:
        """The image posts skipped so far, which are then forgotten: kept as hidden rows, so they count as
        checked in Download coverage and aren't looked at again."""
        with self._removed_lock:
            taken, self.images = list(self.images.values()), {}
        return taken

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

    def fetch_comments(self, post: Post, limit: int = 100, pick: str = "top") -> list[Comment]:
        """``limit`` comments on ``post``, picked by ``pick`` (see ``keep``) from what one request brings.

        Uses the archive's comment tree, which returns a whole thread in one request but can't sort it,
        and cuts it short in the order comments were posted (live, 21 Oct: limit=100 on a 1,065-comment
        thread held 22 of its top 100). A thread with more than TREE_LIMIT comments is cut short and picked
        from what came back. Comments the archive hasn't re-checked yet have no score, so on a post from the
        last day or two the "top" ones are only a sample.
        """
        # Ask for the whole thread when its size is known, so the top ones are really the top ones;
        # half the cap while it isn't (the post's numbers haven't settled).
        known = post.num_comments + 20 if post.num_comments is not None else TREE_LIMIT // 2
        size = min(TREE_LIMIT, max(limit, PAGE, known))
        return keep(self._thread(post, size)[0], limit, pick, post.id)

    def fetch_all_comments(self, post: Post, cap: int = 0, pick: str = "top") -> tuple[list[Comment], bool]:
        """Every comment the archive has on ``post``, or with ``cap`` that many of them picked by ``pick`` from the
        whole thread, and whether what's returned is the whole thread: False when the archive couldn't give all of
        it, or the cap left some out. The whole thread is asked for either way, as the archive can't rank it, and a
        whole tree came back as fast as a 100-comment one live (1.2-3 s for 141-1,123 comments, 21 Oct). Always
        TREE_MAX, not the post's comment count: the archive's count lags its tree, and a big thread asked for its
        count plus some came back with replies collapsed every time (live, 22 Oct: 1,078 counted, 1,264 in the
        tree). A tree that still comes back collapsed or cut short is read in full from the comment search instead,
        a page at a time (see _rest_of_thread); asked for again whole, it came back the same way every time."""
        comments, items, collapsed = self._thread(post, TREE_MAX)
        sent = _number(self.last_params.get("limit"), TREE_MAX)  # smaller if the archive timed out
        whole = items < sent and not collapsed
        if not whole:
            comments, whole = self._rest_of_thread(post, comments)
        kept = keep(comments, cap, pick, post.id)
        return kept, whole and len(kept) == len(comments)

    def _thread(self, post: Post, size: int) -> tuple[list[Comment], int, bool]:
        """The usable comments of a post's tree (parents first), how many items came back, and whether
        any replies were left collapsed."""
        tree = self.get("/api/comments/tree", {"link_id": post.id, "limit": size}, share=TREE_SHARE)
        records = list(_walk(tree))
        return self._comments(post, records, {}), len(records), _collapsed(tree)

    def _rest_of_thread(self, post: Post, have: list[Comment]) -> tuple[list[Comment], bool]:
        """``have`` (part of a post's thread) with the rest of it, from the comment search by post, in posting order,
        and whether every page of it was read. A reply comes after what it answers, so each one's depth follows
        from its parent's."""
        comments = {c.id: c for c in have}
        depths = {c.id: c.depth for c in have}
        cursor, seen, whole, skipped = int(post.created_utc) - 1, set(), False, False
        for _ in range(TREE_MAX // PAGE):
            page = self.get("/api/comments/search", {"link_id": post.id, "sort": "asc", "after": cursor,
                                                     "limit": PAGE}, share=TREE_SHARE)
            sent = _number(self.last_params.get("limit"), PAGE)  # smaller if the archive timed out
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            seen.update(d["id"] for d in new)
            for c in self._comments(post, new, depths):
                comments.setdefault(c.id, c)
            if len(page) < sent:
                whole = not skipped
                break
            last = int(max(_number(d.get("created_utc"), 0) for d in page))
            # On from a second early, so comments sharing the page's last second aren't lost (``seen`` drops the
            # repeats). A full page all from one second can't be read past that way: it's skipped, and the thread
            # isn't called whole.
            if new and last - 1 > cursor:
                cursor = last - 1
            else:
                cursor, skipped = last, True
        return list(comments.values()), whole

    @staticmethod
    def _comments(post: Post, records: Iterable[dict], depths: dict[str, int]) -> list[Comment]:
        """The usable comments among a thread's records, in their order. A record without a depth (the search
        gives none) is one deeper than its parent in ``depths``, which each comment is added to."""
        out: list[Comment] = []
        for d in records:
            if not d.get("id"):
                continue
            parent = _text(d.get("parent_id"))
            depth = d["depth"] if isinstance(d.get("depth"), int) else (
                depths.get(parent[3:], -1) + 1 if parent.startswith("t1_") else 0)
            depths[str(d["id"])] = depth
            body = _text(d.get("body"))
            if body in _GONE or _removed(d):
                continue
            permalink = _text(d.get("permalink")) or f"/r/{post.subreddit}/comments/{post.id}/_/{d['id']}/"
            out.append(Comment(
                id=str(d["id"]),
                post_id=post.id,
                subreddit=post.subreddit,
                parent_id=parent,
                author=_text(d.get("author")) or "[deleted]",
                body=body,
                score=_finite(d.get("score"), int) if _settled(d) else None,
                created_utc=_number(d.get("created_utc"), 0),
                depth=depth,
                permalink="https://www.reddit.com" + permalink,
            ))
        return out


def keep(comments: list[Comment], n: int, pick: str, post_id: str) -> list[Comment]:
    """``n`` of a thread's comments (0: all, as they came): the highest-scored first (unscored ones last), or
    for pick "random" a sample that stays the same for the post: each comment's place comes from a hash of
    its id with the post's, so asking again, or for more, keeps the ones already picked."""
    if not n:
        return comments
    if pick == "random":
        ranked = sorted(comments, key=lambda c: hashlib.blake2b(f"{post_id}/{c.id}".encode(), digest_size=8).digest())
    else:
        ranked = sorted(comments, key=lambda c: -1 if c.score is None else c.score, reverse=True)
    return ranked[:n]


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

