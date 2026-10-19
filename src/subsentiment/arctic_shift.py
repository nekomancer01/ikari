"""Client for the Arctic Shift archive (https://arctic-shift.photon-reddit.com).

Arctic Shift is a free, community-run archive of Reddit posts and comments. It needs no account or
key, which makes it the default source now that Reddit's own logged-out ``.json`` endpoints return
403. API reference: https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md

Differences from Reddit's API that callers should know about:
  * Results come back in date order only. "new", "hot" and "rising" all mean newest first; "top"
    and "controversial" fetch the newest posts in the time range and rank them locally.
  * New items appear within seconds, but ``score`` and ``num_comments`` only settle after about
    36 hours, so very recent posts can show a score of 0 or 1.
  * The archive can keep content that was later removed on Reddit; removed and deleted bodies
    are skipped here.
"""
from __future__ import annotations

import time
from typing import Callable

import requests

from subsentiment.reddit import (
    DEFAULT_USER_AGENT, Comment, Post, ProgressFn, RedditError, StopFn, _post_from, normalize_subreddit,
    subreddit_error,
)

BASE_URL = "https://arctic-shift.photon-reddit.com"

_WINDOWS = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400, "year": 365 * 86400}
_GONE = ("[deleted]", "[removed]")


def _post(d: dict) -> Post:
    p = _post_from(d)
    if not d.get("permalink"):
        p.permalink = f"https://www.reddit.com/r/{p.subreddit}/comments/{p.id}/"
    if p.selftext in _GONE:
        p.selftext = ""
    return p


def _removed(d: dict) -> bool:
    return bool(d.get("removed_by_category"))


class ArcticShiftClient:
    authenticated = False

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        session: requests.Session | None = None,
        min_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = user_agent or DEFAULT_USER_AGENT
        # The archive's limits are dynamic; one request a second keeps well clear of them.
        self.min_interval = min_interval
        self._sleep = sleep
        self._now = now
        self._last_request = 0.0

    # -- HTTP ---------------------------------------------------------------

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path: str, params: dict) -> list:
        for attempt in range(4):
            self._throttle()
            resp = self.session.get(BASE_URL + path, params=params, timeout=60)
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and payload.get("error"):
                    raise RedditError(f"Arctic Shift: {payload['error']}")
                data = payload.get("data") if isinstance(payload, dict) else None
                return data if isinstance(data, list) else []
            if resp.status_code == 429:
                try:
                    retry = float(resp.headers.get("X-RateLimit-Reset") or 0)
                except ValueError:
                    retry = 0.0
                self._sleep(min(retry or 10.0 * (attempt + 1), 60.0))
                continue
            if resp.status_code >= 500:
                self._sleep(5.0 * (attempt + 1))
                continue
            try:
                detail = resp.json().get("error") or ""
            except Exception:
                detail = ""
            raise RedditError(f"Arctic Shift returned HTTP {resp.status_code}" + (f": {detail}" if detail else "."))
        raise RedditError("Arctic Shift is busy or rate limiting; try again in a minute.")

    # -- API ------------------------------------------------------------------

    def fetch_posts(
        self,
        subreddit: str,
        sort: str = "hot",
        time_filter: str = "week",
        limit: int = 100,
        query: str = "",
        progress: ProgressFn | None = None,
        should_stop: StopFn | None = None,
    ) -> list[Post]:
        subreddit = normalize_subreddit(subreddit)
        if error := subreddit_error(subreddit):
            raise RedditError(error)
        base: dict = {"subreddit": subreddit, "sort": "desc"}
        if query:
            base["query"] = query
        if (query or sort in ("top", "controversial")) and time_filter in _WINDOWS:
            base["after"] = int(self._now() - _WINDOWS[time_filter])

        posts: list[Post] = []
        seen: set[str] = set()
        before = None
        while len(posts) < limit:
            if should_stop and should_stop():
                break
            params = dict(base, limit=100)
            if before is not None:
                params["before"] = before
            page = self.get("/api/posts/search", params)
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            for d in new:
                seen.add(d["id"])
                if not _removed(d):
                    posts.append(_post(d))
            if progress:
                progress(f"Fetched {len(posts)} posts from r/{subreddit} (Arctic Shift)")
            if not new or len(page) < 100:
                break
            # Pages overlap by a second so posts sharing a timestamp aren't lost; `seen` drops repeats.
            before = int(min(float(d.get("created_utc") or 0) for d in page)) + 1

        if sort == "top":
            posts.sort(key=lambda p: p.score, reverse=True)
        elif sort == "controversial":
            posts.sort(key=lambda p: (abs(p.upvote_ratio - 0.5) if p.upvote_ratio else 1.0, -p.num_comments))
        return posts[:limit]

    def fetch_comments(self, post: Post, limit: int = 100, sort: str = "top") -> list[Comment]:
        raw: list[dict] = []
        seen: set[str] = set()
        after = None
        while len(raw) < limit:
            params: dict = {"link_id": post.id, "sort": "asc", "limit": 100}
            if after is not None:
                params["after"] = after
            page = self.get("/api/comments/search", params)
            new = [d for d in page if d.get("id") and d["id"] not in seen]
            for d in new:
                seen.add(d["id"])
                raw.append(d)
            if not new or len(page) < 100:
                break
            after = int(max(float(d.get("created_utc") or 0) for d in page)) - 1

        depth: dict[str, int] = {}
        comments: list[Comment] = []
        for d in raw:  # oldest first, so a parent is normally seen before its replies
            parent = d.get("parent_id") or ""
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
                score=int(d.get("score") or 0),
                created_utc=float(d.get("created_utc") or 0),
                depth=depth[d["id"]],
                permalink="https://www.reddit.com" + permalink,
            ))
        if sort == "top":
            comments.sort(key=lambda c: c.score, reverse=True)
        return comments[:limit]
