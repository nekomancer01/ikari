"""A stand-in Arctic Shift server for end-to-end tests: real HTTP on localhost, realistic payloads.

Serves the four endpoints ikari uses (posts search, its day aggregate, posts by id, comment trees) from
generated data, records every request, and can be told to answer some requests with errors first
(the archive's 422 "slow down", 429s, 500s).
"""
from __future__ import annotations

import json
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DAY = 86400


def make_posts(subreddit: str, now: float, days: int = 10, per_day=(6, 0, 12), seed: int = 1) -> list[dict]:
    """Posts for the ``days`` UTC days up to now: per_day cycles through how many each day has (0 is an
    empty day). Every fifth post is removed; posts under 36 hours old aren't settled yet."""
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


class FakeArchive:
    def __init__(self, posts: list[dict]):
        self.posts = posts
        self.requests: list[tuple[str, dict]] = []
        self.faults: dict[str, list[tuple[int, dict]]] = {}  # path -> responses to give before real ones
        self.delay = 0.0  # seconds each request takes, so a test can stop a fetch midway
        self._lock = threading.Lock()
        archive = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                params = {k: v[0] for k, v in parse_qs(url.query).items()}
                status, payload = archive.answer(url.path, params)
                body = json.dumps(payload).encode()
                self.send_response(status)
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

    def answer(self, path: str, q: dict) -> tuple[int, dict]:
        with self._lock:
            self.requests.append((path, q))
            fault = self.faults.get(path)
            if fault:
                return fault.pop(0)
        if self.delay:
            time.sleep(self.delay)
        if path == "/api/posts/search":
            return 200, {"data": self._search(q)}
        if path == "/api/posts/search/aggregate":
            return 200, {"data": self._aggregate(q)}
        if path == "/api/posts/ids":
            ids = set(q["ids"].split(","))
            return 200, {"data": [p for p in self.posts if p["id"] in ids]}
        if path == "/api/comments/tree":
            post = next((p for p in self.posts if p["id"] == q["link_id"].removeprefix("t3_")), None)
            return 200, {"data": comment_tree(post, int(q.get("limit", 100))) if post else []}
        return 400, {"error": f"unknown endpoint {path}"}

    def _matching(self, q: dict) -> list[dict]:
        sub = q.get("subreddit", "").lower()
        after, before = float(q.get("after", 0)), float(q.get("before", 1e12))
        words = q.get("query", "").lower()
        return sorted((p for p in self.posts if p["subreddit"].lower() == sub and after <= p["created_utc"] < before
                       and words in (p["title"] + " " + p["selftext"]).lower()), key=lambda p: p["created_utc"])

    def _search(self, q: dict) -> list[dict]:
        rows = self._matching(q)
        limit = q.get("limit", "100")
        rows = rows[:int(limit)] if limit.isdigit() else rows[:100]
        if fields := q.get("fields"):
            keep = fields.split(",")
            rows = [{k: r[k] for k in keep if k in r} for r in rows]
        return rows

    def _aggregate(self, q: dict) -> list[dict]:
        counts: dict[int, int] = {}
        for p in self._matching(q):
            day = int(p["created_utc"] // DAY * DAY)
            counts[day] = counts.get(day, 0) + 1
        return [{"created_utc": day * 1000, "count": n} for day, n in sorted(counts.items())]
