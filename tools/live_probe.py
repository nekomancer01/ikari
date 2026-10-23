"""Live probe of Arctic Shift endpoints for the fetch rewrite. Read-only, ~60 requests, ~0.5 s apart.

Usage: python tools/live_probe.py [subreddit ...]   (default: classicwow wowforever)
Prints one JSON object per check.
"""
import json
import sys
import time

import requests

BASE = "https://arctic-shift.photon-reddit.com"
S = requests.Session()
S.headers["User-Agent"] = "ikari-probe (+https://github.com/nekomancer01/ikari)"
DAY = 86400


def get(path, **params):
    time.sleep(0.5)
    t = time.monotonic()
    try:
        r = S.get(BASE + path, params=params, timeout=(10, 120))
    except requests.RequestException as e:
        return None, {"error": repr(e), "secs": round(time.monotonic() - t, 2)}
    info = {"status": r.status_code, "secs": round(time.monotonic() - t, 2),
            "remaining": r.headers.get("X-RateLimit-Remaining"), "reset": r.headers.get("X-RateLimit-Reset")}
    try:
        body = r.json()
    except ValueError:
        info["not_json"] = r.text[:200]
        return None, info
    if isinstance(body, dict) and body.get("error"):
        info["error"] = body["error"]
    return (body.get("data") if isinstance(body, dict) else None), info


def walk(items, out, stubs):
    for it in items or ():
        if not isinstance(it, dict):
            continue
        if it.get("kind") == "more":
            stubs.append(it.get("data") or {})
            continue
        d = it.get("data") or {}
        out.append(d)
        rep = d.get("replies")
        if isinstance(rep, dict):
            walk((rep.get("data") or {}).get("children"), out, stubs)


def say(**kw):
    print(json.dumps(kw), flush=True)


def probe(sub):
    now = int(time.time())
    day = now // DAY * DAY - 3 * DAY  # a settled day
    # 1. a day's posts with limit=auto: how many per page
    sizes, cursor, posts = [], day - 1, {}
    for _ in range(20):
        data, info = get("/api/posts/search", subreddit=sub, after=cursor, before=day + DAY, sort="asc", limit="auto")
        if data is None:
            say(check="posts_auto", sub=sub, **info)
            break
        sizes.append((len(data), info["secs"]))
        for d in data:
            posts[d["id"]] = d
        if len(data) < 100:
            break
        cursor = int(max(d["created_utc"] for d in data)) - 1
    say(check="posts_auto", sub=sub, day=day, pages=sizes, posts=len(posts))
    # 2. the day's aggregate
    data, info = get("/api/posts/search/aggregate", subreddit=sub, aggregate="created_utc", frequency="day",
                     after=day, before=day + DAY, limit=2)
    say(check="aggregate", sub=sub, buckets=data, **info)
    if not posts:
        return
    # 3. the biggest thread of the day, and one from the past two weeks if bigger
    big = max(posts.values(), key=lambda d: d.get("num_comments") or 0)
    for back in range(4, 15):
        d0 = now // DAY * DAY - back * DAY
        data, _ = get("/api/posts/search", subreddit=sub, after=d0 - 1, before=d0 + DAY, sort="asc", limit=100,
                      fields="id,num_comments,created_utc")
        for d in data or ():
            if (d.get("num_comments") or 0) > (big.get("num_comments") or 0):
                big = d
        if (big.get("num_comments") or 0) >= 3000:
            break
    pid, counted = big["id"], big.get("num_comments")
    data, info = get("/api/comments/tree", link_id=pid, limit=25000)
    comments, stubs = [], []
    walk(data, comments, stubs)
    stub_ids = [c for s in stubs for c in (s.get("children") or [])]
    say(check="tree_25000", sub=sub, post=pid, num_comments=counted, comments=len(comments), stubs=len(stubs),
        stub_children=len(stub_ids), stub_counts=sum(s.get("count") or 0 for s in stubs), **info)
    for limit in (100, 1000):
        data, info2 = get("/api/comments/tree", link_id=pid, limit=limit)
        c2, s2 = [], []
        walk(data, c2, s2)
        say(check=f"tree_{limit}", sub=sub, post=pid, comments=len(c2), stubs=len(s2),
            stub_children=sum(len(s.get("children") or []) for s in s2), **info2)
        if limit == 100 and s2:
            ids = [c for s in s2 for c in (s.get("children") or [])][:500]
            data, info3 = get("/api/comments/ids", ids=",".join(ids))
            got = data or []
            say(check="comments_ids_expand", sub=sub, asked=len(ids), got=len(got),
                sample_keys=sorted(got[0].keys())[:40] if got else [],
                has_replies_field=bool(got and "replies" in got[0]), **info3)
    # 4. comments/search by link_id, paged in posting order
    seen, cursor, pages = set(), 0, []
    t = time.monotonic()
    for _ in range(80):
        data, info4 = get("/api/comments/search", link_id=pid, sort="asc", limit=100, after=cursor)
        if data is None:
            say(check="comments_search", sub=sub, post=pid, failed=info4)
            break
        pages.append(info4["secs"])
        new = [d for d in data if d["id"] not in seen]
        seen.update(d["id"] for d in data)
        if len(data) < 100 or not new:
            break
        cursor = int(max(d["created_utc"] for d in data)) - 1
    say(check="comments_search", sub=sub, post=pid, comments=len(seen), pages=len(pages),
        secs=round(time.monotonic() - t, 1), slowest=max(pages) if pages else None,
        tree_had=len(comments), tree_ids_missing=len(seen - {c.get("id") for c in comments}))
    data, info5 = get("/api/comments/search", link_id=pid, sort="asc", limit="auto")
    say(check="comments_search_auto", sub=sub, post=pid, got=len(data or []), **info5)


for sub in sys.argv[1:] or ["classicwow", "wowforever"]:
    probe(sub)
