"""Scratch: where test_stop_doesnt_wait_for_a_comment_tree_still_downloading spends its time."""
import cProfile, io, pstats, sys, tempfile, threading, time
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src"), str(Path(__file__).resolve().parents[1] / "tests")]
import test_collect as tc
from subsentiment import collect, store as store_mod

def once(profile=False):
    marks = {}
    class HugeThread(tc.SlowClient):
        def fetch_comments(self, post, limit, pick="top"):
            marks.setdefault("first_tree", time.perf_counter())
            if post.id == "p0":
                time.sleep(3)
            return super().fetch_comments(post, limit)
    orig_each = collect._each_thread
    def each(*a, **k):
        marks.setdefault("threads_in", time.perf_counter())
        try:
            return orig_each(*a, **k)
        finally:
            marks.setdefault("threads_out", time.perf_counter())
    collect._each_thread = each
    commits = [0, 0.0]
    orig = store_mod.Store.commit
    def commit(self):
        t = time.perf_counter(); orig(self); commits[0] += 1; commits[1] += time.perf_counter() - t
    store_mod.Store.commit = commit
    with tempfile.TemporaryDirectory() as d:
        s = tc.store_with(Path(d), [])
        stop = threading.Event()
        def set_stop():
            marks["stop"] = time.perf_counter(); stop.set()
        threading.Timer(0.3, set_stop).start()
        start = time.perf_counter()
        prof = cProfile.Profile() if profile else None
        if prof: prof.enable()
        r = collect.run(HugeThread(posts=[tc.mk(i) for i in range(12)]), s, collect.FetchRequest("python"), should_stop=stop.is_set)
        if prof: prof.disable()
        end = time.perf_counter()
        s.close()
    store_mod.Store.commit = orig
    collect._each_thread = orig_each
    rel = lambda k: f"{marks[k] - start:.2f}" if k in marks else "-"
    print(f"total {end - start:.2f}  first_tree {rel('first_tree')}  stop {rel('stop')}  stop->return "
          f"{end - marks.get('stop', end):.2f}  threads {rel('threads_in')}-{rel('threads_out')}  commits {commits[0]} in {commits[1]:.2f}s  stopped {r.stopped}", flush=True)
    if prof:
        out = io.StringIO(); pstats.Stats(prof, stream=out).sort_stats("cumulative").print_stats(25); print(out.getvalue())

for i in range(int(sys.argv[1]) if len(sys.argv) > 1 else 10):
    once()
once(profile=True)
