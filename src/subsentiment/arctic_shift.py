"""Client for the Arctic Shift archive (https://arctic-shift.photon-reddit.com).

Arctic Shift is a free, community-run archive of Reddit posts and comments. It needs no account or
key. API reference: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md

What callers should know:
  * Posts are searched by date range and come back newest first. There are no Reddit-style sorts.
  * Items are archived within seconds of being posted, then fetched again about 36 hours later.
    Until that second pass, ``score`` and ``num_comments`` are placeholders (usually 1), so they are
    reported as None here. Re-fetching an older range picks up the settled numbers.
  * The archive can keep content that was later removed on Reddit; removed and deleted items are
    skipped.
"""
from __future__ import annotations

import time
from typing import Callable

import requests

from subsentiment import __version__
from subsentiment.reddit import (
    Comment, FetchError, Post, ProgressFn, StopFn, normalize_subreddit, subreddit_error,
)

BASE_URL = "https://arctic-shift.photon-reddit.com"
USER_AGENT = f"ikari/{__version__} (+https://github.com/nekomancer01/ikari)"
SETTLE_SECONDS = 36 * 3600
PAGE = 100  # the API's largest fixed page size
ATTEMPTS = 5
MAX_WAIT = 120.0

_GONE = ("[deleted]", "[removed]")


class RateLimiter:
    """Keeps requests at least ``interval`` seconds apart, and holds them back when the server asks.

    Arctic Shift's limits change with server load. It answers 429 with ``X-RateLimit-Reset`` (seconds
    until the limit resets), and may send ``X-RateLimit-Remaining`` on any response.
    """

    def __init__(self, interval: float = 1.0, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = interval
        self._clock = clock
        self._sleep = sleep
        self._next = 0.0  # earliest time the next request may go out

    def wait(self) -> None:
        delay = self._next - self._clock()
        if delay > 0:
            self._sleep(delay)
        self._next = self._clock() + self.interval

    def hold(self, seconds: float) -> None:
        """Send nothing for the next ``seconds`` (capped at MAX_WAIT)."""
        self._next = max(self._next, self._clock() + min(max(seconds, 0.0), MAX_WAIT))

    def observe(self, headers) -> None:
        """Pause until the reset when a response says no requests are left."""
        if _number(headers.get("X-RateLimit-Remaining"), 1.0) <= 0:
            self.hold(_number(headers.get("X-RateLimit-Reset"), 10.0))


def _number(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _settled(d: dict) -> bool:
    """True once the archive has re-fetched the item, so score and comment count are real."""
    if (d.get("_meta") or {}).get("retrieved_2nd_on"):
        return True
    return _number(d.get("retrieved_on"), 0) - _number(d.get("created_utc"), 0) >= SETTLE_SECONDS


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


class ArcticShiftClient:
    def __init__(self, session: requests.Session | None = None, limiter: RateLimiter | None = None):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.limiter = limiter or RateLimiter()

    # -- HTTP ---------------------------------------------------------------

    def get(self, path: str, params: dict) -> list:
        """One API call with retries: waits on 429, backs off on 5xx and network errors, and asks
        for smaller pages when the archive says the query timed out."""
        params = dict(params)
        for attempt in range(ATTEMPTS):
            backoff = min(5.0 * 2 ** attempt, MAX_WAIT)
            self.limiter.wait()
            try:
                resp = self.session.get(BASE_URL + path, params=params, timeout=60)
            except (requests.ConnectionError, requests.Timeout):
                if attempt == ATTEMPTS - 1:
                    raise
                self.limiter.hold(backoff)
                continue
            self.limiter.observe(resp.headers)
            error = _error_text(resp)
            if resp.status_code == 200 and not error:
                data = resp.json().get("data")
                return data if isinstance(data, list) else []
            if "timed out" in error.lower() and params.get("limit", PAGE) > 10:
                params["limit"] = max(10, params.get("limit", PAGE) // 2)
                continue
            if resp.status_code == 429:
                self.limiter.hold(_number(resp.headers.get("X-RateLimit-Reset"), backoff))
                continue
            if resp.status_code >= 500:
                self.limiter.hold(backoff)
                continue
            if resp.status_code == 200:
                raise FetchError(f"Arctic Shift: {error}")
            raise FetchError(f"Arctic Shift returned HTTP {resp.status_code}" + (f": {error}" if error else "."))
        raise FetchError("Arctic Shift is busy or rate limiting. Try again in a few minutes.")

    # -- API ------------------------------------------------------------------

    def fetch_posts(
        self,
        subreddit: str,
        after: float | None = None,
        before: float | None = None,
        limit: int = 100,
        query: str = "",
        progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
    ) -> list[Post]:
        """Posts created in [after, before), newest first, up to ``limit``."""
        subreddit = normalize_subreddit(subreddit)
        if error := subreddit_error(subreddit):
            raise FetchError(error)
        base: dict = {"subreddit": subreddit, "sort": "desc", "limit": PAGE}
        if query:
            base["query"] = query
        if after is not None:
            base["after"] = int(after)

        posts: list[Post] = []
        seen: set[str] = set()
        cursor = int(before) if before is not None else None
        while len(posts) < limit:
            if should_stop and should_stop():
                break
            params = dict(base) if cursor is None else dict(base, before=cursor)
            page = self.get("/api/posts/search", params)
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            for d in new:
                seen.add(d["id"])
                if not d.get("removed_by_category"):
                    posts.append(_post(d))
            if progress:
                progress(f"Fetched {min(len(posts), limit):,} posts from r/{subreddit}")
            if not new:  # a short page is not proof of the end: the archive may have shrunk it
                break
            # The next page ends where this one did, plus a second so posts sharing that timestamp
            # aren't lost; `seen` drops the repeats.
            cursor = int(min(_number(d.get("created_utc"), 0) for d in page)) + 1
        return posts[:limit]

    def fetch_comments(self, post: Post, limit: int = 100) -> list[Comment]:
        """Up to ``limit`` comments on ``post``, highest score first (oldest first while unscored)."""
        raw: list[dict] = []
        seen: set[str] = set()
        cursor = None
        while len(raw) < limit:
            params: dict = {"link_id": post.id, "sort": "asc", "limit": PAGE}
            if cursor is not None:
                params["after"] = cursor
            page = self.get("/api/comments/search", params)
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            for d in new:
                seen.add(d["id"])
                raw.append(d)
            if not new:
                break
            cursor = int(max(_number(d.get("created_utc"), 0) for d in page)) - 1

        depth: dict[str, int] = {}
        comments: list[Comment] = []
        for d in raw:  # oldest first, so a parent is normally seen before its replies
            parent = d.get("parent_id") or ""
            if isinstance(d.get("depth"), int):
                depth[d["id"]] = d["depth"]
            else:
                depth[d["id"]] = depth.get(parent[3:], 0) + 1 if parent.startswith("t1_") else 0
            body = d.get("body") or ""
            if body in _GONE:
                continue
            permalink = d.get("permalink") or f"/r/{post.subreddit}/comments/{post.id}/_/{d['id']}/"
            comments.append(Comment(
                id=d["id"],
                post_id=post.id,
                subreddit=post.subreddit,
                parent_id=parent,
                author=d.get("author") or "[deleted]",
                body=body,
                score=int(d.get("score") or 0) if _settled(d) else None,
                created_utc=_number(d.get("created_utc"), 0),
                depth=depth[d["id"]],
                permalink="https://www.reddit.com" + permalink,
            ))
        comments.sort(key=lambda c: -1 if c.score is None else c.score, reverse=True)
        return comments[:limit]


def _error_text(resp) -> str:
    try:
        payload = resp.json()
    except Exception:
        return ""
    return str(payload.get("error") or "") if isinstance(payload, dict) else ""
