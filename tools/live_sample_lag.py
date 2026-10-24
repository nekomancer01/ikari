"""Live: the real MainWindow downloads a range with Keep: Sample into a throwaway DB; reports UI gaps, what the
window's thread was doing in each stall, request sizes/timings/failures, and how the fetch ended.
  QT_QPA_PLATFORM=offscreen python live_sample_lag.py SUB RANGE MINUTES PAGE   (e.g. linux_gaming year 20 overview)"""
import collections, faulthandler, os, re, sys, tempfile, threading, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import requests
from PySide6.QtCore import QElapsedTimer, QSettings, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox

LOG = []  # (path, limit, secs, bytes, status or error)
_get = requests.Session.get
def get(self, url, *a, **k):
    t = time.perf_counter(); p = (k.get("params") or {})
    try:
        r = _get(self, url, *a, **k)
    except Exception as e:
        LOG.append((url.split("/api/")[-1], p.get("limit"), time.perf_counter() - t, 0, type(e).__name__)); raise
    LOG.append((url.split("/api/")[-1], p.get("limit"), time.perf_counter() - t, len(r.content), r.status_code)); return r
requests.Session.get = get
JSON = []
_json = requests.Response.json
def rjson(self, *a, **k):
    t = time.thread_time(); out = _json(self, *a, **k); JSON.append((len(self.content), time.thread_time() - t)); return out
requests.Response.json = rjson

def main():
    faulthandler.enable(all_threads=True)  # a crash prints every thread's stack
    sub, rng, minutes, page = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
    tmp = tempfile.mkdtemp(prefix="samplelag-")
    app = QApplication([]); app.setOrganizationName("samplelag"); app.setApplicationName("samplelag")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, tmp + "/cfg")
    QSettings().setValue("updates/auto", False)
    warn = []
    QMessageBox.warning = lambda *a, **k: warn.append(a[2] if len(a) > 2 else a)
    from subsentiment.gui import main_window as mw
    w = mw.MainWindow(tmp + "/db.db"); w.resize(1280, 760); w.show()
    f = w.fetch_form
    QTest.mouseClick(w.rail.buttons["fetch"], Qt.MouseButton.LeftButton)
    f.subreddit.setText(sub); f.include_comments.setChecked(True)
    f.range.setCurrentIndex(f.range.findData(rng))
    f.comment_cap.pick.setCurrentIndex(f.comment_cap.pick.findData("sample"))
    gaps = []; clock = QElapsedTimer(); clock.start(); last = [0]
    def beat():
        now = clock.elapsed(); gaps.append((now - last[0], now)); last[0] = now
    hb = QTimer(); hb.timeout.connect(beat); hb.start(1)
    stalls = open(tmp + "/stalls.txt", "w+")
    arm = QTimer(); arm.timeout.connect(lambda: faulthandler.dump_traceback_later(0.15, file=stalls))
    if not os.environ.get("NO_STALLS"):  # stall dumps off, to rule them out
        arm.start(10)
    t0 = time.monotonic(); nxt = 0
    QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
    w.show_page(page)
    status = lambda: " / ".join(l.text() for l in w.findChildren(QLabel) if l.isVisible() and re.search(r"Day |omments|Added|Waiting|Arctic", l.text()))[:200]
    while w.fetch_job.busy and time.monotonic() - t0 < minutes * 60:
        app.processEvents(); time.sleep(0.002)
        if time.monotonic() > nxt:
            nxt = time.monotonic() + 30
            recent = [g for g, at in gaps if at > clock.elapsed() - 30000]
            print(f"{time.monotonic()-t0:6.0f}s gaps(30s): worst {max(recent or [0])} ms, >100ms {sum(g>100 for g in recent)}, >500ms {sum(g>500 for g in recent)}"
                  f" | requests {len(LOG)}, failed {sum(1 for x in LOG if x[4] != 200)} | {status()}", flush=True)
    busy = w.fetch_job.busy
    if busy:
        w._stop_fetch()
    faulthandler.cancel_dump_traceback_later(); arm.stop()
    print("\n== ended", "by time limit (stopped)" if busy else "by itself", f"after {time.monotonic()-t0:.0f}s")
    print("status:", status()); print("warnings:", warn)
    g = sorted(x for x, _ in gaps); print("UI gaps worst", g[-15:], " >100ms", sum(x > 100 for x in g), " >500ms", sum(x > 500 for x in g), " >2s", sum(x > 2000 for x in g))
    by = collections.defaultdict(list)
    for path, lim, secs, size, st in LOG: by[path].append((secs, size, st))
    for path, xs in by.items():
        s = sorted(x[0] for x in xs); b = sorted(x[1] for x in xs)
        print(f"{path:22} n={len(xs):5} secs p50 {s[len(s)//2]:.1f} max {s[-1]:.1f} | KB p50 {b[len(b)//2]//1024} max {b[-1]//1024} total MB {sum(b)/2**20:.0f} | not 200: {collections.Counter(x[2] for x in xs if x[2] != 200)}")
    big = sorted(JSON, reverse=True)[:5]; print("biggest JSON parses (KB, CPU s):", [(n // 1024, round(t, 2)) for n, t in big], " total parse CPU s", round(sum(t for _, t in JSON), 1))
    stalls.seek(0); txt = stalls.read(); main_tops = collections.Counter(); other = collections.Counter()
    for d in txt.split("Timeout (")[1:]:
        for t in re.split(r"\n(?=Thread 0x|Current thread 0x)", d):
            fr = [re.sub(r'.*/(src/subsentiment/|python3\.\d+/|site-packages/)', '', l.strip()) for l in t.splitlines() if l.strip().startswith("File")]
            if not fr: continue
            key = " < ".join(fr[:3])
            (main_tops if any("live_sample_lag" in x for x in fr) else other)[key] += 1
    print("stall dumps:", txt.count("Timeout ("))
    print("window thread in stalls:"); [print(" ", c, k) for k, c in main_tops.most_common(10)]
    print("other threads in stalls:"); [print(" ", c, k) for k, c in other.most_common(10)]
    print("data dir:", tmp)
    sys.stdout.flush(); os._exit(0)

if __name__ == "__main__":
    main()
