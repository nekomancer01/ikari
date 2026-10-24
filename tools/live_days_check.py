"""Times a whole-range download against the real Arctic Shift, as the app runs one, into a throwaway database.

Run from the repo root:  python tools/live_days_check.py [subreddit] [days] [comments: 0|1]
The range ends two days ago, so every day in it is over and can be marked complete. Run it from two checkouts
(before and after a change) to compare: it prints the time, the requests each endpoint took, the posts and comments
saved and how many days were marked complete, which should match between the two.
"""
from __future__ import annotations

import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from subsentiment import collect  # noqa: E402
from subsentiment.arctic_shift import ArcticShiftClient  # noqa: E402
from subsentiment.store import Store  # noqa: E402


def main() -> None:
    sub = sys.argv[1] if len(sys.argv) > 1 else "linux_gaming"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
    comments = (sys.argv[3] if len(sys.argv) > 3 else "0") == "1"
    store = Store(Path(tempfile.mkdtemp()) / "check.db")
    client = ArcticShiftClient()
    client.limiter.on_wait = lambda secs, why: secs and print(f"  {why}; waiting {secs:.0f} s")
    sent: Counter = Counter()
    real_get = client.session.get

    def counting_get(url, *a, **kw):
        sent[url.rsplit("/api/", 1)[-1]] += 1
        return real_get(url, *a, **kw)
    client.session.get = counting_get
    before = int(time.time() // 86400 * 86400) - 86400
    req = collect.FetchRequest(sub, after=before - days * 86400, before=before, limit=None,
                               include_comments=comments, comments_per_post=50)
    last = [""]

    def progress(done, total, msg):
        if msg.split(":")[0] != last[0]:
            last[0] = msg.split(":")[0]
            print(f"  {time.strftime('%H:%M:%S')} {msg}")
    start = time.time()
    result = collect.run(client, store, req, progress=progress)
    took = time.time() - start
    print(f"\nr/{sub}, {days} days, comments {'on' if comments else 'off'}: {took:.1f} s")
    print(f"  {result}")
    print(f"  requests: {dict(sent)}")
    print(f"  saved: {len(store.posts(sub))} posts, {len(store.comments(sub))} comments; "
          f"{len(store.complete_keys('posts', sub))} of {days} days complete")


if __name__ == "__main__":
    main()
