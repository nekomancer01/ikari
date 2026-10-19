"""Minimal Reddit client for reading subreddit posts and comments.

Two modes:
  * anonymous: the public ``.json`` endpoints on www.reddit.com. No setup, but
    Reddit rate-limits these hard and, since mid-2026, refuses most of them (HTTP 403).
  * app-only OAuth: a "script" app's client id/secret from
    https://www.reddit.com/prefs/apps, used with the client_credentials grant
    against oauth.reddit.com. Read-only, no Reddit username/password needed.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Iterator

import requests

DEFAULT_USER_AGENT = "desktop:subsentiment:v0.1 (subreddit sentiment explorer)"

LISTING_SORTS = ("hot", "new", "top", "rising", "controversial")
SEARCH_SORTS = ("relevance", "hot", "top", "new", "comments")
TIME_FILTERS = ("hour", "day", "week", "month", "year", "all")

_SUBREDDIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_]{1,20}")
_SUBREDDIT_URL = re.compile(r"^(?:https?://)?(?:[a-z]+\.)?reddit\.com/", re.IGNORECASE)

ProgressFn = Callable[[str], None]
StopFn = Callable[[], bool]


class RedditError(Exception):
    pass


@dataclass
class Post:
    id: str
    subreddit: str
    title: str
    selftext: str
    author: str
    score: int
    upvote_ratio: float
    num_comments: int
    created_utc: float
    permalink: str
    url: str

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.selftext}".strip()


@dataclass
class Comment:
    id: str
    post_id: str
    subreddit: str
    parent_id: str
    author: str
    body: str
    score: int
    created_utc: float
    depth: int
    permalink: str


def normalize_subreddit(name: str) -> str:
    """Turn "r/Python", "/r/Python/" or a pasted subreddit URL into "Python"; "" when nothing is left."""
    name = _SUBREDDIT_URL.sub("", name.strip()).lstrip("/")
    if name.lower().startswith("r/"):
        name = name[2:]
    return name.split("/", 1)[0].strip()


def subreddit_error(name: str) -> str | None:
    """Why a normalised name can't be fetched, or None when it looks valid."""
    if not name:
        return "Enter a subreddit name."
    if not _SUBREDDIT_NAME.fullmatch(name):
        return f"“{name}” isn't a valid subreddit name: use 2–21 letters, digits or underscores."
    return None


def _post_from(d: dict) -> Post:
    return Post(
        id=d["id"],
        subreddit=d.get("subreddit") or "",
        title=d.get("title") or "",
        selftext=d.get("selftext") or "",
        author=d.get("author") or "[deleted]",  # null for deleted accounts
        score=int(d.get("score") or 0),
        upvote_ratio=float(d.get("upvote_ratio") or 0.0),
        num_comments=int(d.get("num_comments") or 0),
        created_utc=float(d.get("created_utc") or 0),
        permalink="https://www.reddit.com" + d.get("permalink", ""),
        url=d.get("url", ""),
    )


def flatten_comments(children: list, post_id: str, subreddit: str, depth: int = 0) -> Iterator[Comment]:
    """Walk a comment tree depth-first, skipping "load more" stubs."""
    for child in children:
        if child.get("kind") != "t1":
            continue
        d = child["data"]
        body = d.get("body") or ""
        if body not in ("[deleted]", "[removed]"):
            yield Comment(
                id=d["id"],
                post_id=post_id,
                subreddit=subreddit,
                parent_id=d.get("parent_id", ""),
                author=d.get("author") or "[deleted]",
                body=body,
                score=int(d.get("score") or 0),
                created_utc=float(d.get("created_utc") or 0),
                depth=depth,
                permalink="https://www.reddit.com" + d.get("permalink", ""),
            )
        replies = d.get("replies")
        if isinstance(replies, dict):
            yield from flatten_comments(replies["data"]["children"], post_id, subreddit, depth + 1)


class RedditClient:
    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        client_id: str | None = None,
        client_secret: str | None = None,
        session: requests.Session | None = None,
        min_interval: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.user_agent = user_agent or DEFAULT_USER_AGENT
        self.client_id = client_id or None
        self.client_secret = client_secret or None
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = self.user_agent
        # Anonymous access gets ~10 req/min; OAuth gets ~100 req/min.
        if min_interval is None:
            min_interval = 0.7 if self.authenticated else 6.0
        self.min_interval = min_interval
        self._sleep = sleep
        self._last_request = 0.0
        self._token: str | None = None
        self._token_expiry = 0.0

    @property
    def authenticated(self) -> bool:
        return bool(self.client_id and self.client_secret)

    # -- HTTP ---------------------------------------------------------------

    def _ensure_token(self) -> None:
        if self._token and time.time() < self._token_expiry - 60:
            return
        resp = self.session.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise RedditError(f"Reddit login failed (HTTP {resp.status_code}). Check the client id and secret.")
        payload = resp.json()
        if "access_token" not in payload:
            raise RedditError(f"Reddit login failed: {payload.get('error', payload)}")
        self._token = payload["access_token"]
        self._token_expiry = time.time() + float(payload.get("expires_in", 3600))

    def _throttle(self) -> None:
        wait = self._last_request + self.min_interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last_request = time.monotonic()

    def get(self, path: str, params: dict | None = None) -> dict | list:
        params = dict(params or {}, raw_json=1)
        for attempt in range(4):
            self._throttle()
            if self.authenticated:
                self._ensure_token()
                url = "https://oauth.reddit.com" + path
                headers = {"Authorization": f"bearer {self._token}"}
            else:
                url = "https://www.reddit.com" + path + ".json"
                headers = {}
            resp = self.session.get(url, params=params, headers=headers, timeout=30)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:
                try:
                    retry = float(resp.headers.get("Retry-After") or 0)
                except ValueError:  # an HTTP date instead of seconds
                    retry = 0.0
                retry = retry or 10.0 * (attempt + 1)
                self._sleep(min(retry, 60.0))
                continue
            if resp.status_code == 401 and self.authenticated:
                self._token = None
                continue
            if resp.status_code == 403:
                hint = (
                    " Reddit now blocks most anonymous requests; add Reddit API credentials in Settings."
                    if not self.authenticated else " The subreddit may be private or quarantined."
                )
                raise RedditError(f"Access denied (HTTP 403) for {path}.{hint}")
            if resp.status_code == 404:
                raise RedditError(f"Not found (HTTP 404): {path}. Check the subreddit name.")
            if resp.status_code >= 500:
                self._sleep(5.0 * (attempt + 1))
                continue
            raise RedditError(f"HTTP {resp.status_code} for {path}")
        raise RedditError(f"Gave up on {path} after repeated rate limiting or server errors.")

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
        if query:
            path = f"/r/{subreddit}/search"
            base = {"q": query, "restrict_sr": 1, "sort": sort if sort in SEARCH_SORTS else "relevance", "t": time_filter}
        else:
            path = f"/r/{subreddit}/{sort}"
            base = {"t": time_filter} if sort in ("top", "controversial") else {}

        posts: list[Post] = []
        seen: set[str] = set()
        after = None
        while len(posts) < limit:
            if should_stop and should_stop():
                break
            params = dict(base, limit=min(100, limit - len(posts)))
            if after:
                params["after"] = after
            data = self.get(path, params)
            listing = data.get("data", {}) if isinstance(data, dict) else {}
            children = listing.get("children", [])
            for child in children:
                if child.get("kind") == "t3" and child["data"]["id"] not in seen:
                    seen.add(child["data"]["id"])
                    posts.append(_post_from(child["data"]))
            if progress:
                progress(f"Fetched {len(posts)} posts from r/{subreddit}")
            after = listing.get("after")
            if not after or not children:
                break
        return posts[:limit]

    def fetch_comments(self, post: Post, limit: int = 100, sort: str = "top") -> list[Comment]:
        data = self.get(f"/r/{post.subreddit}/comments/{post.id}", {"limit": limit, "sort": sort})
        if not isinstance(data, list) or len(data) < 2:
            return []
        comments = list(flatten_comments(data[1]["data"]["children"], post.id, post.subreddit))
        return comments[:limit]
