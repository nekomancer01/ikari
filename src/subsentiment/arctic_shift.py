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

import math
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
TIMEOUT_TRIES = 3  # query timeouts on one request before the caller is asked to narrow it
MIN_WINDOW = 600  # seconds; a timing-out window is halved down to this
TREE_LIMIT = 300  # comments asked for per thread; big trees are slow for the archive and trip its limits
TREE_MAX = 25000  # the most comments the tree endpoint returns in one request
UNKNOWN_THREAD = 1000  # what fetch_all_comments asks for when a post's comment count isn't known yet
COUNT_CHUNK_DAYS = 31  # days per aggregate request; a timing-out chunk is split down to one day
DAY = 86400

_GONE = ("[deleted]", "[removed]")

WaitFn = Callable[[float, str], None]


class QueryTimeout(FetchError):
    """The archive gave up on a query as too slow, even with small pages. A narrower date window
    usually works."""


class RateLimiter:
    """Keeps requests at least ``interval`` seconds apart, and holds them back when the server asks.

    Arctic Shift's limits change with server load. It answers 429 with ``X-RateLimit-Reset`` (seconds
    until the limit resets) or ``X-RateLimit-Reset-At`` (a Unix time), and may send
    ``X-RateLimit-Remaining`` on any response. Each 429 doubles the spacing (up to MAX_INTERVAL);
    successful requests ease it back to the base interval.
    """

    def __init__(self, interval: float = BASE_INTERVAL, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep, wall: Callable[[], float] = time.time):
        self.base = self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._wall = wall
        self._next = 0.0  # earliest time the next request may go out
        self.on_wait: WaitFn | None = None  # told about long pauses, so the UI can say why it's idle
        self._lock = threading.Lock()  # one limiter can be shared by jobs on several threads

    def wait(self) -> None:
        with self._lock:
            delay = self._next - self._clock()
            if delay > 0:
                if delay >= 3 and self.on_wait:
                    self.on_wait(delay, self._reason)
                self._sleep(delay)
            self._next = self._clock() + self.interval

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

    def observe(self, headers) -> None:
        """Pause until the reset when a response says no requests are left."""
        if _number(headers.get("X-RateLimit-Remaining"), 1.0) <= 0:
            self.hold(self.reset_after(headers, 10.0), "Arctic Shift's request limit was reached")

    _reason = "Arctic Shift is busy"


def _number(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _settled(d: dict) -> bool:
    """True once the archive has re-fetched the item, so score and comment count are real."""
    if (d.get("_meta") or {}).get("retrieved_2nd_on"):
        return True
    return _number(d.get("retrieved_on"), 0) - _number(d.get("created_utc"), 0) >= SETTLE_SECONDS


def _removed(d: dict) -> bool:
    meta = d.get("_meta") or {}
    return bool(d.get("removed_by_category") or meta.get("removal_type") or meta.get("was_deleted_later"))


def _post(d: dict) -> Post:
    settled = _settled(d)
    subreddit = d.get("subreddit") or ""
    selftext = d.get("selftext") or ""
    return Post(
        id=d["id"],
        subreddit=subreddit,
        title=d.get("title") or "",
        selftext="" if selftext in _GONE else selftext,
        author=d.get("author") or "[deleted]",
        score=int(d.get("score") or 0) if settled else None,
        upvote_ratio=float(d.get("upvote_ratio") or 0.0) if settled else None,
        num_comments=int(d.get("num_comments") or 0) if settled else None,
        created_utc=_number(d.get("created_utc"), 0),
        permalink="https://www.reddit.com" + (d.get("permalink") or f"/r/{subreddit}/comments/{d['id']}/"),
        url=d.get("url") or "",
    )


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
    def __init__(self, session: requests.Session | None = None, limiter: RateLimiter | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.limiter = limiter or RateLimiter()

    # -- HTTP ---------------------------------------------------------------

    def get(self, path: str, params: dict, shrink: bool = True) -> list:
        """One API call with retries: waits on 429, backs off on 5xx and network errors, and on a
        query timeout backs off and asks for smaller pages (unless ``shrink`` is False, for requests
        whose ``limit`` isn't a page size), raising QueryTimeout if that doesn't help."""
        params = dict(params)
        self.last_params = params  # what was finally sent, after any shrinking
        timeouts = 0
        for attempt in range(ATTEMPTS):
            backoff = min(5.0 * 2 ** attempt, MAX_WAIT)
            self.limiter.wait()
            try:
                resp = self.session.get(BASE_URL + path, params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == ATTEMPTS - 1:
                    raise
                self.limiter.hold(backoff, "Couldn't reach Arctic Shift")
                continue
            self.limiter.observe(resp.headers)
            error = _error_text(resp)
            if resp.status_code == 200 and not error:
                self.limiter.ok()
                data = resp.json().get("data")
                return data if isinstance(data, list) else []
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
    ) -> list[Post]:
        """Up to ``limit`` posts created in [after, before), spread evenly over the range, oldest first.

        The range is cut into equal slices (several per day for short ranges, about one a day for
        long ones) and each slice gets an equal share of ``limit``. Without ``top``, a slice's share
        is its earliest posts not in ``skip``, so a repeat fetch picks up posts it didn't have. With
        ``top``, each slice's share is the highest-scored posts from a pool pulled from across the
        slice; only settled posts have scores, so in a range that is all recent posts, nothing ranks.

        ``on_posts`` is given each batch of posts as soon as it is downloaded (every request, outside top
        mode, where posts can only be ranked at the end), so a caller can show them while the rest comes.
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

        n = slot_count(after, before, limit, top)
        windows = slices(after, before, n)
        picked: list[list[Post]] = [[] for _ in windows]
        spare: list[list[Post]] = [[] for _ in windows]  # top mode: ranked candidates not picked yet
        more = [True] * len(windows)  # the slice may hold posts we haven't read
        cursors = [lo for lo, _ in windows]
        what = "Ranking" if top else "Collecting"
        for i, ((lo, hi), want) in enumerate(zip(windows, quotas(limit, len(windows)))):
            if should_stop and should_stop():
                break
            if top:
                ranked = sorted(self._top_pool(base, lo, hi, should_stop), key=_light_rank, reverse=True)
                picked[i], spare[i] = ranked[:want], ranked[want:]
                more[i] = False
            else:
                picked[i], cursors[i], more[i] = self._earliest(base, lo, hi, want, skip, should_stop,
                                                                on_posts)
            if progress:
                got = sum(map(len, picked))
                progress(f"{what} posts from r/{subreddit}: part {i + 1} of {len(windows)}, {got:,} so far")

        # Quiet slices (night hours, a slow week) leave the total short. Share what's missing among the
        # slices that had more to give, a few rounds at most.
        for _ in range(3):
            short = limit - sum(map(len, picked))
            if top:
                open_ = [i for i, extra in enumerate(spare) if extra]
            else:
                open_ = [i for i, m in enumerate(more) if m]
            if short <= 0 or not open_ or (should_stop and should_stop()):
                break
            for i, want in zip(open_, quotas(short, len(open_))):
                if want <= 0:
                    continue
                if top:
                    picked[i] += spare[i][:want]
                    spare[i] = spare[i][want:]
                else:
                    have = skip | {p.id for p in picked[i]}
                    extra, cursors[i], more[i] = self._earliest(base, cursors[i], windows[i][1], want, have,
                                                                should_stop, on_posts)
                    picked[i] += extra
        if top:
            picked = self._hydrate(picked, spare, should_stop)
        posts = [p for group in picked for p in group]
        posts.sort(key=lambda p: p.created_utc)
        if top and on_posts and posts:
            on_posts(posts)
        return posts

    def _page(self, base: dict, lo: int, hi: int, size) -> tuple[list[dict], int]:
        """Posts from [lo, hi) and the end of the window actually searched: when the archive times
        out, the window is halved (down to MIN_WINDOW) and only its first part is searched."""
        while True:
            try:
                return self.get("/api/posts/search", dict(base, after=lo, before=hi, limit=size)), hi
            except QueryTimeout:
                if hi - lo <= MIN_WINDOW:
                    raise
                hi = lo + (hi - lo) // 2

    def _earliest(self, base, lo, hi, want, skip, should_stop,
                  on_posts=None) -> tuple[list[Post], int, bool]:
        """The first ``want`` usable posts in [lo, hi) that aren't in ``skip``, where to carry on
        reading from, and whether the slice may hold more."""
        out: list[Post] = []
        seen: set[str] = set()
        cursor = lo
        for _ in range(SLOT_PAGES):
            # A few spare for removed posts; a full page when saved posts may need skipping.
            size = PAGE if skip else min(PAGE, want - len(out) + 5)
            page, end = self._page(base, cursor, hi, size)
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            fresh: list[Post] = []
            for d in new:
                seen.add(d["id"])
                if d["id"] not in skip and not _removed(d) and len(out) < want:
                    out.append(_post(d))
                    fresh.append(out[-1])
                    cursor = max(cursor, int(_number(d.get("created_utc"), 0)) - 1)
            if fresh and on_posts:
                on_posts(fresh)
            if len(out) >= want or (should_stop and should_stop()):
                return out, cursor, True
            if end < hi and (len(page) < size or not new):  # only part of the window was searched
                cursor = end - 1
                continue
            if len(page) < size or not new:
                return out, cursor, False
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
            for d in self._page(dict(base, fields=TOP_FIELDS), start, end, PAGE)[0]:
                if d.get("id"):
                    pool[d["id"]] = d
        return list(pool.values())

    def _hydrate(self, picked: list[list[dict]], spare: list[list[dict]], should_stop) -> list[list[Post]]:
        """Full copies of each slice's picks, in one batch by id. Picks that turn out to be removed are
        replaced by that slice's next-best spares."""
        want = [len(group) for group in picked]
        ranked = [group + extra[:math.ceil(len(group) * TOP_SPARE)] for group, extra in zip(picked, spare)]
        full = {p.id: p for p in self.fetch_posts_by_ids([d["id"] for group in ranked for d in group], should_stop)}
        return [[full[d["id"]] for d in group if d["id"] in full][:n] for group, n in zip(ranked, want)]

    def fetch_posts_by_ids(self, ids: Iterable[str], should_stop: StopFn | None = None) -> list[Post]:
        """Current copies of the given posts, IDS_BATCH per request; removed ones are left out."""
        ids = list(dict.fromkeys(ids))
        out: list[Post] = []
        for i in range(0, len(ids), IDS_BATCH):
            if should_stop and should_stop():
                break
            out.extend(_post(d) for d in self.get("/api/posts/ids", {"ids": ",".join(ids[i:i + IDS_BATCH])})
                       if d.get("id") and not _removed(d))
        return out

    def day_counts(self, subreddit: str, after: float, before: float,
                   should_stop: StopFn | None = None) -> dict[int, int]:
        """Posts the archive holds for each UTC day in [after, before), as {day start: count}, from its
        aggregate endpoint. The counts include posts later removed on Reddit, which fetching skips. Days
        the archive couldn't count (it times out on very busy subreddits) are left out."""
        subreddit = normalize_subreddit(subreddit)
        if error := subreddit_error(subreddit):
            raise FetchError(error)
        first, end = int(after // DAY * DAY), int(math.ceil(before / DAY) * DAY)
        todo = [(lo, min(lo + COUNT_CHUNK_DAYS * DAY, end)) for lo in range(first, end, COUNT_CHUNK_DAYS * DAY)]
        out: dict[int, int] = {}
        while todo:
            if should_stop and should_stop():
                break
            lo, hi = todo.pop(0)
            params = {"subreddit": subreddit, "aggregate": "created_utc", "frequency": "day",
                      "after": lo, "before": hi, "limit": (hi - lo) // DAY + 1}
            try:
                buckets = self.get("/api/posts/search/aggregate", params, shrink=False)
            except QueryTimeout:
                if hi - lo > DAY:  # try the halves; a single day that times out stays unknown
                    mid = lo + (hi - lo) // DAY // 2 * DAY
                    todo[:0] = [(lo, mid), (mid, hi)]
                continue
            for day in range(lo, hi, DAY):
                out[day] = 0  # a day with no bucket had no posts
            for b in buckets:
                when, n = _bucket_time(b), _number((b or {}).get("count", (b or {}).get("doc_count")), None)
                if when is not None and n is not None and lo <= when < hi:
                    day = int(when // DAY * DAY)
                    out[day] = out.get(day, 0) + int(n)
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
            body = d.get("body") or ""
            if not d.get("id") or body in _GONE or _removed(d):
                continue
            permalink = d.get("permalink") or f"/r/{post.subreddit}/comments/{post.id}/_/{d['id']}/"
            comments.append(Comment(
                id=d["id"],
                post_id=post.id,
                subreddit=post.subreddit,
                parent_id=d.get("parent_id") or "",
                author=d.get("author") or "[deleted]",
                body=body,
                score=int(d.get("score") or 0) if _settled(d) else None,
                created_utc=_number(d.get("created_utc"), 0),
                depth=int(d["depth"]) if isinstance(d.get("depth"), int) else 0,
                permalink="https://www.reddit.com" + permalink,
            ))
        return comments, items, _collapsed(tree)


def _walk(items: list, depth: int = 0):
    """Comments in a Reddit-style tree ({"kind": "t1", "data": {..., "replies": Listing}}), parents
    before replies. "more" stubs (collapsed replies the archive didn't expand) are skipped."""
    for item in items or ():
        if not isinstance(item, dict) or item.get("kind") != "t1":
            continue
        d = dict(item.get("data") or {})
        d.setdefault("depth", depth)
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


def _error_text(resp) -> str:
    try:
        payload = resp.json()
    except Exception:
        return ""
    return str(payload.get("error") or "") if isinstance(payload, dict) else ""
