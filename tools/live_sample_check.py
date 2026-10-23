"""Compares a day's comment sentiment from the statistical sample (Keep: Sample) with every comment, live.

Run from the repo root:  python tools/live_sample_check.py [days] [subreddit ...]
Downloads every post and comment of each settled day into a throwaway database, works out which comments the day's
sample keeps (collect.Sample), and prints both sets of shares and the biggest gap; the sample promises 5 points or
less on about 19 days in 20. On the first day it also runs a real Sample download into a second database and checks
it kept the same comments. Nothing of yours is touched.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from subsentiment import collect  # noqa: E402
from subsentiment.arctic_shift import ArcticShiftClient  # noqa: E402
from subsentiment.store import SAMPLED, Store  # noqa: E402

DAY = 86400
LABELS = ("positive", "negative", "neutral")


def shares(rows) -> dict[str, float]:
    return {k: 100 * sum(r["label"] == k for r in rows) / max(len(rows), 1) for k in LABELS}


def main() -> None:
    args = sys.argv[1:]
    n_days = int(args.pop(0)) if args and args[0].isdigit() else 7
    subs = args or ["linux_gaming", "Deltarune", "linux"]
    client = ArcticShiftClient()
    full = Store(Path(tempfile.mkdtemp()) / "full.db")
    today = int(time.time() // DAY * DAY)
    days = [today - k * DAY for k in range(3, 3 + n_days)]  # settled: counted by the archive
    gaps = []
    for sub in subs:
        for day in days:
            collect.run(client, full, collect.FetchRequest(sub, after=day, before=day + DAY, limit=None, days=(day,),
                                                           comments_per_post=0, fill_threads=True))
            totals = full.comment_totals(sub, day, day + DAY)
            plan = collect.Sample(totals)
            threads: dict[str, list] = {}
            for r in full.comments(sub):
                if r["post_id"] in totals:
                    threads.setdefault(r["post_id"], []).append(r)
            rows = [r for rs in threads.values() for r in rs]
            kept = [r for pid, rs in threads.items() for r in plan.keep(pid, rs)]
            a, b = shares(rows), shares(kept)
            gap = max(abs(a[k] - b[k]) for k in LABELS)
            gaps.append(gap)
            print(f"{sub:14} {time.strftime('%d %b', time.gmtime(day))}  counted {plan.population:6,}  in trees "
                  f"{len(rows):6,}  need {plan.size:4}  kept {len(kept):4}{' whole' if plan.whole else ''}  |  all "
                  + " ".join(f"{a[k]:4.1f}" for k in LABELS) + "  sample " + " ".join(f"{b[k]:4.1f}" for k in LABELS)
                  + f"  gap {gap:.1f}", flush=True)
            if sub == subs[0] and day == days[0]:
                real = Store(Path(tempfile.mkdtemp()) / "sample.db")
                collect.run(client, real, collect.FetchRequest(sub, after=day, before=day + DAY, limit=None,
                                                               days=(day,), include_comments=False))
                ids = tuple(real.open_threads(sub, [day], cap=SAMPLED))
                collect.run(client, real, collect.FetchRequest(sub, comment_posts=ids, fill_threads=True,
                                                               comment_pick=collect.SAMPLE))
                got = {r["id"] for r in real.comments(sub)}
                want = {r["id"] for r in kept}
                print(f"  a real Sample download kept {len(got)}, {len(got & want)} of them the same as above "
                      f"({len(ids)} threads open, the archive can change in between)", flush=True)
    inside = sum(g <= 5 for g in gaps)
    print(f"days within 5 points: {inside} of {len(gaps)}; worst gap {max(gaps):.1f} points (shares as "
          + "/".join(LABELS) + ")")


if __name__ == "__main__":
    main()
