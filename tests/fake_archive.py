"""A stand-in Arctic Shift server for end-to-end tests: real HTTP on localhost, realistic payloads.

Serves the four endpoints ikari uses (posts search, its day aggregate, posts by id, comment trees) from
generated data and records every request. It enforces the API's documented constraints
(https://github.com/ArthurHeitmann/arctic_shift/blob/master/api/README.md) the way the real server
answers them, so a request ikari shouldn't send fails here too:

- search ``limit`` is 1-100 or "auto" (default 25), ``fields`` only the selectable ones, ids at most 500
  per lookup, a comment tree needs ``link_id`` and a ``limit`` of 1-25000 and collapses what's past it
  into "more" stubs, a date aggregate needs ``frequency``; anything else is an HTTP 400 with an error.
- a rate limit: ``rate`` requests per ``window`` seconds, with X-RateLimit-Remaining/Reset headers on
  every answer and a 429 once it's spent ("Rate limits are calculated dynamically"; a couple of requests
  a second is fine).
- busy subreddits (``busy``) time out the way the archive does: keyword search answers
  "Query timed out" (HTTP 200), a page over a window holding more than BUSY_PAGE posts or a day aggregate
  over more than BUSY_AGG_DAYS days answers 422 "Timeout. Maybe slow down a bit".
- a day aggregate's buckets hold UTC days but are keyed two hours early, at 22:00Z the day before.
- scores are 1 and num_comments 0 until a post is 36 hours old; removed posts stay in the archive.

It can also be told to answer some requests with errors first (``fail``), or to answer slowly
(``delay``), so a test can stop a fetch midway.
"""
from __future__ import annotations

import json
import math
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DAY = 86400
SELECTABLE = {"author", "author_fullname", "author_flair_text", "created_utc", "distinguished", "id",
              "retrieved_on", "subreddit", "subreddit_id", "score", "crosspost_parent", "link_flair_text",
              "num_comments", "over_18", "post_hint", "selftext", "spoiler", "title", "url"}
BUSY_PAGE = 15  # a busy subreddit times out on a window holding more posts than this
BUSY_AGG_DAYS = 7
CLOCK_SLACK = 0.03
SLOW_DOWN = (422, {"error": "Timeout. Maybe slow down a bit"})


def make_posts(subreddit: str, now: float, days: int = 10, per_day=(6, 12, 0), seed: int = 1) -> list[dict]:
    """Posts for the ``days`` UTC days up to now: per_day cycles through how many each day has (0 is an
    empty day). Yesterday always has posts, so "Past 24 hours" finds some even just after midnight UTC,
    when today has none yet. Every fifth post is removed; posts under 36 hours old aren't settled yet."""
    rng = random.Random(seed)
    today = int(now // DAY * DAY)
    words = ["love this", "great update", "terrible bug", "so annoying", "works fine", "meh", "thanks all"]
    out = []
    n = 0
    for d in range(days):
        day = today - d * DAY
        for k in range(per_day[d % len(per_day)]):
            created = day + 3600 + k * 600
            if created > now:
                continue
            n += 1
            pid = f"{subreddit[:2].lower()}{n:04d}"
            settled = now - created > 36 * 3600
            post = {"id": pid, "subreddit": subreddit, "title": f"{rng.choice(words)} #{n}",
                    "selftext": rng.choice(["", "details inside", "[removed]"]), "author": f"user{n}",
                    "score": rng.randint(2, 500) if settled else 1, "upvote_ratio": 0.9,
                    "num_comments": rng.randint(0, 6) if settled else 0, "created_utc": created,
                    "retrieved_on": created + (40 * 3600 if settled else 60),
                    "permalink": f"/r/{subreddit}/comments/{pid}/x/", "url": ""}
            if n % 5 == 0:
                post["removed_by_category"] = "moderator"
            out.append(post)
    return out


def comment_tree(post: dict, limit: int) -> list[dict]:
    n = max(post["num_comments"], 2)
    items = []
    for i in range(min(n, limit)):
        data = {"id": f"{post['id']}c{i}", "link_id": "t3_" + post["id"], "parent_id": "t3_" + post["id"],
                "author": f"c{i}", "body": ["nice", "awful", "ok"][i % 3], "score": i,
                "created_utc": post["created_utc"] + 60 * (i + 1),
                "retrieved_on": post["retrieved_on"], "replies": ""}
        items.append({"kind": "t1", "data": data})
    return items


class Rejected(Exception):
    def __init__(self, status: int, error: str, headers: dict | None = None):
        super().__init__(error)
        self.status, self.error, self.headers = status, error, headers or {}


def _int(q: dict, key: str, lo: int, hi: int | None, default=None, allow_auto=False):
    raw = q.get(key)
    if raw is None:
        return default
    if allow_auto and raw == "auto":
        return "auto"
    if raw == "" and hi is None:
        return None  # "empty means no limit"
    try:
        value = int(raw)
    except ValueError:
        raise Rejected(400, f"Invalid {key}: {raw}")
    if value < lo or (hi is not None and value > hi):
        raise Rejected(400, f"{key} must be between {lo} and {hi}" if hi else f"{key} must be >= {lo}")
    return value


def _date(q: dict, key: str) -> float | None:
    raw = q.get(key)
    if raw is None:
        return None
    try:
        v = float(raw)
    except ValueError:
        raise Rejected(400, f"Invalid date for {key}: {raw}")  # (the real API also takes ISO and 2d; ikari sends epochs)
    return v / 1000 if v > 1e11 else v


class FakeArchive:
    def __init__(self, posts: list[dict], rate: tuple[int, float] = (60, 1.0), busy=()):
        self.posts = posts
        self.rate, self.window_start, self.used = rate, time.perf_counter(), 0
        self.busy = {b.lower() for b in busy}
        self.rejected: list[tuple[str, dict, int, str]] = []  # every 4xx sent: path, params, status, error
        self.requests: list[tuple[str, dict]] = []
        self.faults: dict[str, list[tuple[int, dict]]] = {}  # path -> responses to give before real ones
        self.delay = 0.0  # seconds each request takes, so a test can stop a fetch midway
        self._lock = threading.Lock()
        archive = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                status, payload, headers = archive.answer(url.path, params)
                body = json.dumps(payload).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def paths(self) -> list[str]:
        with self._lock:
            return [p for p, _ in self.requests]

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()

    def fail(self, path: str, *responses: tuple[int, dict]) -> None:
        self.faults.setdefault(path, []).extend(responses)

    def answer(self, path: str, q: dict) -> tuple[int, dict, dict]:
        with self._lock:
            self.requests.append((path, q))
            headers = self._spend()
            fault = self.faults.get(path)
            if fault:
                status, payload = fault.pop(0)
                return status, payload, headers
        if self.delay:
            time.sleep(self.delay)
        try:
            if headers.get("X-RateLimit-Remaining") == "-1":
                raise Rejected(429, "Too many requests", {"X-RateLimit-Remaining": "0",
                                                          "X-RateLimit-Reset": headers["X-RateLimit-Reset"]})
            return 200, {"data": self._route(path, q)}, headers
        except Rejected as e:
            with self._lock:
                self.rejected.append((path, q, e.status, e.error))
            return e.status, {"error": e.error}, {**headers, **e.headers}

    def _spend(self) -> dict:
        """Count a request against the rate limit; the headers to send with its answer."""
        budget, window = self.rate
        now = time.perf_counter()
        # A request up to CLOCK_SLACK early still counts as the next window: Windows' time.monotonic,
        # which the app's limiter waits by, only ticks every ~16 ms on Python 3.12.
        if now - self.window_start >= window - CLOCK_SLACK:
            self.window_start, self.used = now, 0
        self.used += 1
        reset = max(0.0, window - (now - self.window_start))
        return {"X-RateLimit-Remaining": str(budget - self.used) if self.used <= budget else "-1",
                "X-RateLimit-Reset": f"{math.ceil(reset * 100) / 100:.2f}"}  # rounded up, never early

    def violations(self) -> list[tuple[str, dict, int, str]]:
        """Requests the real API would refuse for their parameters (not rate limits or timeouts)."""
        return [r for r in self.rejected if r[2] == 400]

    def _route(self, path: str, q: dict):
        if path == "/api/posts/search":
            return self._search(q)
        if path == "/api/posts/search/aggregate":
            return self._aggregate(q)
        if path == "/api/posts/ids":
            ids = [i.removeprefix("t3_") for i in q.get("ids", "").split(",") if i]
            if not ids or len(ids) > 500:
                raise Rejected(400, "ids: between 1 and 500 ids")
            self._fields(q)
            wanted = set(ids)
            return [p for p in self.posts if p["id"] in wanted]
        if path == "/api/comments/tree":
            if not q.get("link_id"):
                raise Rejected(400, "link_id is required")
            limit = _int(q, "limit", 1, 25000, default=50)
            post = next((p for p in self.posts if p["id"] == q["link_id"].removeprefix("t3_")), None)
            return comment_tree(post, limit) if post else []
        raise Rejected(400, f"unknown endpoint {path}")

    def _fields(self, q: dict) -> list[str] | None:
        if "fields" not in q:
            return None
        keep = q["fields"].split(",")
        if bad := [f for f in keep if f not in SELECTABLE]:
            raise Rejected(400, f"Invalid fields: {','.join(bad)}")
        return keep

    def _matching(self, q: dict) -> list[dict]:
        sub = q.get("subreddit", "").lower()
        after, before = float(q.get("after", 0)), float(q.get("before", 1e12))
        words = q.get("query", "").lower()
        return sorted((p for p in self.posts if p["subreddit"].lower() == sub and after <= p["created_utc"] < before
                       and words in (p["title"] + " " + p["selftext"]).lower()), key=lambda p: p["created_utc"])

    def _search(self, q: dict) -> list[dict]:
        limit = _int(q, "limit", 1, 100, default=25, allow_auto=True)
        keep = self._fields(q)
        if q.get("sort") not in (None, "asc", "desc"):
            raise Rejected(400, "sort must be asc or desc")
        after, before = _date(q, "after"), _date(q, "before")
        if q.get("subreddit", "").lower() in self.busy:
            if q.get("query") or q.get("title") or q.get("selftext"):
                raise Rejected(200, "Query timed out")
        rows = self._matching(q)
        if q.get("subreddit", "").lower() in self.busy and len(rows) > BUSY_PAGE:
            raise Rejected(*SLOW_DOWN[:1], SLOW_DOWN[1]["error"])
        if q.get("sort") == "desc":
            rows.reverse()
        rows = rows[:limit if limit != "auto" else 300]
        if keep:
            rows = [{k: r[k] for k in keep if k in r} for r in rows]
        return rows

    def _aggregate(self, q: dict) -> list[dict]:
        if q.get("aggregate") not in ("created_utc", "author", "subreddit"):
            raise Rejected(400, "aggregate must be created_utc, author or subreddit")
        if q["aggregate"] == "created_utc" and not q.get("frequency"):
            raise Rejected(400, "frequency is required with aggregate=created_utc")
        _int(q, "limit", 1, None)
        after, before = _date(q, "after") or 0, _date(q, "before") or time.time()
        if q.get("subreddit", "").lower() in self.busy and before - after > BUSY_AGG_DAYS * DAY:
            raise Rejected(*SLOW_DOWN[:1], SLOW_DOWN[1]["error"])
        counts: dict[int, int] = {}
        for p in self._matching(q):
            day = int(p["created_utc"] // DAY * DAY)
            counts[day] = counts.get(day, 0) + 1
        # Keyed the way the archive keys them: at its own midnight, UTC+2, so 22:00Z the day before. The
        # bucket still holds the UTC day.
        return [{"created_utc": (day - 7200) * 1000, "count": n} for day, n in sorted(counts.items())]
