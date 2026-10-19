"""Fetches a small sample from the real Arctic Shift API and prints what came back.

Run from the repo root:  python tools/live_check.py [subreddit] [days]
                         python tools/live_check.py --app [subreddit] [days]
Nothing is saved (--app runs a whole fetch job, as the app does, into a throwaway database). Useful after changing arctic_shift.py, since the tests only use a fake archive.
"""
from __future__ import annotations

import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from subsentiment.arctic_shift import ArcticShiftClient  # noqa: E402


def app_run(sub: str, days: int) -> None:
    """The app's own fetch job: posts, score refresh and comments, as the Fetch button runs it."""
    import tempfile

    from subsentiment import collect
    from subsentiment.store import Store

    store = Store(Path(tempfile.mkdtemp()) / "check.db")
    client = ArcticShiftClient()
    calls: list[tuple[str, float]] = []  # (endpoint, seconds the server took)
    real_get = client.session.get

    def timed_get(url, *a, **kw):
        t = time.time()
        try:
            return real_get(url, *a, **kw)
        finally:
            calls.append((url.rsplit("/api/", 1)[-1], time.time() - t))
    client.session.get = timed_get
    req = collect.FetchRequest(sub, after=time.time() - days * 86400, limit=30, comments_per_post=50)
    for run in (1, 2):
        calls.clear()
        start = time.time()
        result = collect.run(client, store, req, progress=lambda d, t, m: print(f"  {m}") if not t or d % 10 == 0 else None)
        took = time.time() - start
        print(f"\nRun {run}: {result} in {took:.0f} s")
        for endpoint in sorted({e for e, _ in calls}):
            secs = [s for e, s in calls if e == endpoint]
            print(f"  {endpoint}: {len(secs)} requests, {sum(secs):.0f} s waiting on the server "
                  f"(slowest {max(secs):.1f} s)")
        print(f"  the rest (spacing between requests, pauses, saving): {took - sum(s for _, s in calls):.0f} s")
    rows = store.posts(sub)
    with_comments = {r["post_id"] for r in store.comments(sub)}
    print(f"{len(rows)} posts saved, {len(with_comments)} with comments, {len(store.comments(sub))} comments in all")


def main() -> None:
    if sys.argv[1:2] == ["--app"]:
        app_run(sys.argv[2] if len(sys.argv) > 2 else "Deltarune", int(sys.argv[3]) if len(sys.argv) > 3 else 7)
        return
    sub = sys.argv[1] if len(sys.argv) > 1 else "Minecraft"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    client = ArcticShiftClient()
    client.limiter.on_wait = lambda secs, why: print(f"  {why}; waiting {secs:.0f} s")
    sent = 0
    real_get = client.session.get

    def counting_get(*a, **kw):
        nonlocal sent
        sent += 1
        return real_get(*a, **kw)
    client.session.get = counting_get

    before = time.time() - 2 * 86400  # leave out the last two days, whose scores aren't in yet
    after = before - days * 86400
    for top in (False, True):
        sent, start = 0, time.time()
        posts = client.fetch_posts(sub, after, before, limit=50, top=top)
        per_day = Counter(datetime.fromtimestamp(p.created_utc, timezone.utc).date() for p in posts)
        scored = [p for p in posts if p.score is not None]
        print(f"\n{'Top by score' if top else 'Spread across days'}: {len(posts)} posts, {sent} requests, "
              f"{time.time() - start:.0f} s")
        print("  per day:", ", ".join(f"{d:%b %d} {n}" for d, n in sorted(per_day.items())))
        print(f"  scored: {len(scored)}/{len(posts)}, best: {max((p.score for p in scored), default='-')}")
    best = max(posts, key=lambda p: p.num_comments or 0)
    sent = 0
    comments = client.fetch_comments(best, 50)
    print(f"\nComments on \"{best.title[:50]}\" ({best.num_comments} on Reddit): {len(comments)} in {sent} request(s)")


if __name__ == "__main__":
    main()
