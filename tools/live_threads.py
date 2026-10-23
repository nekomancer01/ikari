"""Live check for PR #164: big threads read whole. Read-only. Run from the repo root on the PR branch:
python tools/live_threads.py [subreddit ...]   (default: classicwow wowforever)
"""
import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "src"))
from subsentiment import arctic_shift as a  # noqa: E402

DAY = 86400
c = a.ArcticShiftClient()
calls = collections.Counter()
statuses = collections.Counter()
_get = c.session.get


def counted(url, **kw):
    r = _get(url, **kw)
    calls[url.split(".com")[-1]] += 1
    statuses[r.status_code] += 1
    return r


c.session.get = counted


def say(**kw):
    print(json.dumps(kw), flush=True)


for sub in sys.argv[1:] or ["classicwow", "wowforever"]:
    now = int(time.time())
    posts = {}
    for back in range(3, 17):
        d0 = now // DAY * DAY - back * DAY
        for d in c.get("/api/posts/search", {"subreddit": sub, "after": d0 - 1, "before": d0 + DAY,
                                             "sort": "asc", "limit": 100}):
            posts[d["id"]] = d
    ranked = sorted(posts.values(), key=lambda d: d.get("num_comments") or 0, reverse=True)
    picks = ranked[:3] + ranked[len(ranked) // 4:len(ranked) // 4 + 1]  # 3 biggest and one ordinary thread
    say(sub=sub, posts=len(posts), picks=[(d["id"], d.get("num_comments")) for d in picks])
    for d in picks:
        post = a._post(d)
        calls.clear()
        tree, items, collapsed = c._thread(post, a.TREE_MAX)
        t = time.monotonic()
        calls.clear()
        got, whole = c.fetch_all_comments(post)
        ids = {x.id for x in got}
        orphans = sum(1 for x in got if x.parent_id.startswith("t1_") and x.parent_id[3:] not in ids)
        say(sub=sub, post=post.id, num_comments=post.num_comments, tree=len(tree), tree_items=items,
            collapsed=collapsed, got=len(got), unique=len(ids), whole=whole, secs=round(time.monotonic() - t, 1),
            requests=dict(calls), orphans=orphans, max_depth=max((x.depth for x in got), default=0))
say(statuses=dict(statuses))
