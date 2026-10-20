"""Use the app the way a person does and record what they would see, step by step.

    python tools/qa_walkthrough.py --out qa/                 # against a busy, realistic fake archive
    python tools/qa_walkthrough.py --out qa/ --live          # against the real Arctic Shift (needs its network)
    python tools/qa_walkthrough.py --out qa/ --scale 1.5     # rendered at a real 150% device pixel ratio

Starts the real MainWindow with a throwaway database and settings, then types into the fetch form and
presses the buttons: a first fetch, a fetch of a second subreddit while the first is on screen, Stop in the
middle of a fetch, every tab and View option, CSV export, the Download coverage window with its Download all
buttons, and deleting a subreddit. While each fetch runs it samples once a second what the database holds and
what the window shows, and a 20 ms heartbeat timer measures how long the window stops responding. Times every
HTTP request, every SQLite commit and every sentiment call, so slow steps can be split into network and local
work. Writes a PNG per step, walkthrough.json and walkthrough.md.

Unlike the tests, the fake archive runs at the app's real request pacing, answers with a latency like the real
one (--latency) and holds a busy subreddit with hundreds of posts a day. With --scale the window renders at
that device pixel ratio (as Windows does at 150%), not at ratio 1 with a bigger font as tools/ui_matrix.py does.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def parse() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default="qa-walkthrough")
    ap.add_argument("--live", action="store_true", help="use the real Arctic Shift")
    ap.add_argument("--scale", type=float, default=1.0, help="device pixel ratio, e.g. 1.5 for Windows 150%%")
    ap.add_argument("--screen", default="1920x1200", help="physical screen size")
    ap.add_argument("--real-screen", action="store_true", help="use the desktop instead of an offscreen screen")
    ap.add_argument("--theme", default="dark", choices=["dark", "light", "system"])
    ap.add_argument("--small", default="python", help="a quieter subreddit")
    ap.add_argument("--busy", default="AskReddit", help="a busy subreddit")
    ap.add_argument("--latency", type=float, default=0.6, help="fake archive: seconds per answer")
    ap.add_argument("--timeout", type=float, default=600, help="seconds to let one fetch run")
    ap.add_argument("--stop-after", type=float, default=20, help="seconds into a fetch to press Stop")
    return ap.parse_args()


ARGS = parse()
OUT = Path(ARGS.out)
OUT.mkdir(parents=True, exist_ok=True)

# A screen of the given physical size at ratio 1, then Qt's own scaling on top: that gives the window a real
# device pixel ratio (fonts, pixmaps and the grain rasterised at 1.5x), as on a Windows screen at 150%.
pw, ph = (int(v) for v in ARGS.screen.split("x"))
cfg = Path(tempfile.mkdtemp()) / "screens.json"
cfg.write_text(json.dumps({"screens": [{"name": "qa", "x": 0, "y": 0, "width": pw, "height": ph,
                                        "logicalDpi": 96, "logicalBaseDpi": 96, "dpr": 1}]}))
if not ARGS.real_screen:
    os.environ["QT_QPA_PLATFORM"] = f"offscreen:configfile={cfg}"
if ARGS.scale != 1:
    os.environ["QT_SCALE_FACTOR"] = str(ARGS.scale)
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import requests  # noqa: E402
from PySide6.QtCore import QEvent, QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox, QPushButton  # noqa: E402

from subsentiment import arctic_shift, sentiment  # noqa: E402
from subsentiment.gui import main_window as mw  # noqa: E402
from subsentiment.gui import native, theme  # noqa: E402
from subsentiment.store import Store  # noqa: E402

import inspect  # noqa: E402

from subsentiment.gui import coverage as cov  # noqa: E402

WORKAROUNDS = []
if "_fit(open_day" in inspect.getsource(cov.Cell) and "open_day" not in inspect.signature(cov.Cell._fit).parameters:
    # main at d2c8c28 has two Cell._fit methods (two PRs merged together), so the window crashes on open.
    # Put both back under one name so the rest of the window can still be walked through.
    _side_fit = cov.Cell._fit

    def _both_fits(self, open_day=None):
        if open_day is not None:
            if hasattr(self, "bar"):
                pol = self.bar.sizePolicy()
                pol.setRetainSizeWhenHidden(not open_day)
                self.bar.setSizePolicy(pol)
            self.lay.setContentsMargins(*((8, 2, 12, 2) if open_day else (8, 7, 12, 9)))
            self.lay.setSpacing(0 if open_day else 5)
        if hasattr(self, "side"):
            _side_fit(self)

    cov.Cell._fit = _both_fits
    WORKAROUNDS.append("coverage.Cell._fit defined twice on main: patched so the window opens")

DAY = 86400
log: list[dict] = []  # one entry per step
http = {"n": 0, "secs": 0.0, "status": collections.Counter(), "slowest": 0.0, "bad": []}
local = {"commits": 0, "commit_secs": 0.0, "scores": 0, "score_secs": 0.0}
stalls: list[float] = []  # gaps between heartbeat ticks over 200 ms, in ms

_get = requests.Session.get


def timed_get(self, url, **kw):
    t = time.perf_counter()
    try:
        r = _get(self, url, **kw)
    finally:
        dt = time.perf_counter() - t
        http["n"] += 1
        http["secs"] += dt
        http["slowest"] = max(http["slowest"], dt)
    http["status"][r.status_code] += 1
    if r.status_code >= 400 and len(http["bad"]) < 30:
        http["bad"].append([r.status_code, url.rsplit("/api/", 1)[-1], kw.get("params"), r.text[:120]])
    return r


requests.Session.get = timed_get
_commit, _score = Store.commit, sentiment.score


def timed_commit(self):
    t = time.perf_counter()
    _commit(self)
    local["commits"] += 1
    local["commit_secs"] += time.perf_counter() - t


def timed_score(text):
    t = time.perf_counter()
    out = _score(text)
    local["scores"] += 1
    local["score_secs"] += time.perf_counter() - t
    return out


Store.commit = timed_commit
sentiment.score = timed_score
import subsentiment.collect as collect  # noqa: E402

collect.sentiment.score = timed_score


def snapshot_counters() -> dict:
    return {"http": http["n"], "http_secs": http["secs"], "status": dict(http["status"]), **local,
            "stalls": len(stalls)}


def delta(before: dict) -> dict:
    now = snapshot_counters()
    out = {k: round(now[k] - before[k], 3) for k in ("http", "http_secs", "commits", "commit_secs", "scores",
                                                      "score_secs", "stalls")}
    out["status"] = {str(k): v - before["status"].get(k, 0) for k, v in now["status"].items()
                     if v - before["status"].get(k, 0)}
    out["worst_stall_ms"] = round(max(stalls[before["stalls"]:], default=0))
    return out


def pump(secs: float) -> None:
    end = time.monotonic() + secs
    while time.monotonic() < end:
        QApplication.processEvents()
        # processEvents outside exec() never runs deleteLater; the app's own event loop does.
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.005)


def shot(widget, name: str) -> str:
    pump(0.3)
    path = OUT / f"{len(list(OUT.glob('*.png'))):02d}-{name}.png"
    widget.grab().save(str(path))
    beat["last"] = time.perf_counter()  # the walkthrough's own grab isn't a freeze of the app
    if not QGuiApplication.platformName().startswith("offscreen"):  # what the screen really shows
        widget.window().raise_()
        pump(0.2)
        QGuiApplication.primaryScreen().grabWindow(0).save(str(path.with_name(path.stem + "-screen.png")))
        beat["last"] = time.perf_counter()
    return path.name


def step(name: str):
    def wrap(fn):
        def run(*a, **k):
            entry = {"step": name, "notes": [], "shots": []}
            log.append(entry)
            doing[0] = name.split(":")[0]
            before = snapshot_counters()
            t = time.perf_counter()
            try:
                fn(entry, *a, **k)
            except Exception:
                entry["error"] = traceback.format_exc(limit=6)
                print(entry["error"], file=sys.stderr)
            entry["secs"] = round(time.perf_counter() - t, 2)
            entry["cost"] = delta(before)
            print(f"[{entry['secs']:7.1f}s] {name}: {'; '.join(entry['notes'])[:300]}", flush=True)
        return run
    return wrap


def db_counts(sub: str) -> tuple[int, int]:
    # Read-only and raw: Store.close() runs a TRUNCATE checkpoint, which waits up to 5 s for the fetch's
    # writes, and those waits on the GUI thread would be the walkthrough's own freezes, not the app's.
    import sqlite3

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=0.5)
    try:
        return tuple(con.execute(f"select count(*) from {t} where subreddit = ? collate nocase", (sub,)).fetchone()[0]
                     for t in ("posts", "comments"))
    except sqlite3.OperationalError:  # no database yet
        return 0, 0
    finally:
        con.close()


def visible(w) -> dict:
    return {"heading": w.heading.text(), "posts_rows": w.posts_tab.view.model().rowCount(),
            "comments_rows": w.comments_tab.view.model().rowCount(), "progress": w.progress_msg.text()[:140],
            "tab_posts": tab_text("posts"), "tab_comments": tab_text("comments")}


def busy(job_owner, job="fetch_job", old="thread") -> bool:
    """Whether a fetch (or coverage count) is running: BackgroundJob since #60, a QThread attribute before."""
    if hasattr(job_owner, job):
        return getattr(job_owner, job).busy
    return getattr(job_owner, "_" + old if old == "count_thread" else old) is not None


def run_fetch(entry: dict, w, sub: str, start, stop_after: float | None = None, every: float = 1.0,
              shots_at=(3, 15, 60)) -> None:
    """Start a fetch with ``start()`` and watch it to the end, sampling the database and the window."""
    samples = entry.setdefault("samples", [])
    t0 = time.monotonic()
    start()
    pump(0.05)
    if not busy(w):
        entry["notes"].append("fetch didn't start: " + w.progress_msg.text())
        return
    next_sample, pending_shots, stopped_at = 0.0, list(shots_at), None
    while busy(w):
        el = time.monotonic() - t0
        if el >= next_sample:
            p, c = db_counts(sub)
            samples.append({"t": round(el, 1), "db_posts": p, "db_comments": c, **visible(w)})
            next_sample += every
        if pending_shots and el >= pending_shots[0]:
            entry["shots"].append(shot(w, f"{entry['step'].split(':')[0]}-at-{pending_shots.pop(0)}s"))
        if stop_after is not None and stopped_at is None and el >= stop_after:
            stopped_at = el
            entry["before_stop"] = {"t": round(el, 1), **dict(zip(("db_posts", "db_comments"), db_counts(sub))),
                                    **visible(w)}
            QTest.mouseClick(w.stop_btn, Qt.MouseButton.LeftButton)
            entry["after_click"] = visible(w)
        if el > ARGS.timeout and not entry.get("timed_out"):
            entry["timed_out"] = True
            entry["notes"].append(f"still running after {ARGS.timeout:.0f}s; pressing Stop")
            w._stop_fetch()
            stop_after, stopped_at = 0, el
        if stopped_at is not None and el - stopped_at > 120:
            entry["notes"].append(f"STOP DIDN'T END THE FETCH: still running {el - stopped_at:.0f}s after Stop")
            entry["stop_never_ended"] = True
            break
        QApplication.processEvents()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        time.sleep(0.005)
    entry["fetch_secs"] = round(time.monotonic() - t0, 1)
    if stopped_at is not None:
        entry["stop_to_idle_secs"] = round(time.monotonic() - t0 - stopped_at, 1)
    pump(1.5)  # the drip and fade-in
    p, c = db_counts(sub)
    entry["end"] = {"db_posts": p, "db_comments": c, **visible(w)}
    entry["shots"].append(shot(w, f"{entry['step'].split(':')[0]}-done"))
    # What a person would notice: rows saved but never shown while fetching.
    live = [s for s in samples if s["db_posts"] or s["db_comments"]]
    if live:
        first_saved = next(s["t"] for s in live)
        shown = [s for s in samples if s["posts_rows"] + s["comments_rows"] > 0 and s["t"] >= first_saved]
        entry["first_row_saved_at"] = first_saved
        entry["first_row_shown_at"] = shown[0]["t"] if shown else None
        lag = [s["db_posts"] - s["posts_rows"] for s in samples if s["heading"].lower() == f"r/{sub}".lower()]
        entry["max_posts_saved_not_shown"] = max(lag, default=None)
    entry["notes"].append(f"{entry['fetch_secs']}s, db {p} posts/{c} comments, window shows "
                          f"{entry['end']['posts_rows']}/{entry['end']['comments_rows']}: "
                          f"{entry['end']['progress']}")


def type_into(widget, text: str) -> None:
    widget.setFocus()
    widget.selectAll()
    QTest.keyClicks(widget, text)


def click(button) -> None:
    QTest.mouseClick(button, Qt.MouseButton.LeftButton)


# Pages and drawers: a navigation rail since #79 (Fetch and Saved open drawers), tabs and a sidebar before.
OLD_TABS = ("overview", "posts", "comments")


def pages() -> list[str]:
    return list(w.page_widgets) if hasattr(w, "rail") else list(OLD_TABS)


def go(key: str) -> None:
    """Show a page the way a person would: its rail button, or its tab."""
    if hasattr(w, "rail"):
        if w.rail.page() != key:
            click(w.rail.buttons[key])
    else:
        w.tabbar.setCurrentIndex(OLD_TABS.index(key))


def current_page() -> str:
    return w.rail.page() if hasattr(w, "rail") else OLD_TABS[w.tabbar.currentIndex()]


def open_drawer(key: str) -> None:
    """Open the Fetch or Saved drawer from the rail, if this main has one and it isn't open already."""
    """("" closes the open one, by clicking its button again.)"""
    if hasattr(w, "rail") and w._drawer_key() != key:
        click(w.rail.buttons[key or w._drawer_key()])
        pump(0.3)


def tab_text(key: str) -> str:
    if hasattr(w, "rail"):
        return w.rail.buttons[key].text()
    return w.tabbar.tabText(OLD_TABS.index(key))


def timeline_chart():
    return w.timeline_page.body if hasattr(w, "timeline_page") else w.overview.timeline


# -- the walkthrough ---------------------------------------------------------------------------------------

print("starting Qt", flush=True)
app = QApplication([])
print(f"Qt platform {app.platformName()}, screen {app.primaryScreen().geometry()}", flush=True)
app.setOrganizationName("ikari-qa")
app.setApplicationName("ikari-qa")
TMP = Path(tempfile.mkdtemp())
QSettings.setDefaultFormat(QSettings.Format.IniFormat)
QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(TMP))
QSettings().setValue("updates/auto", False)
QSettings().setValue("ui/theme", ARGS.theme)
# What gui/app.py does before opening the window: without it the window has no stylesheet or fonts.
theme.load_fonts(app)
native.set_appearance(ARGS.theme)
theme.apply(app, ARGS.theme)
DB = str(TMP / "qa.db")
dialogs: list[str] = []
QMessageBox.warning = lambda *a, **k: dialogs.append(f"warning: {a[1]}: {a[2]}") or QMessageBox.StandardButton.Ok
QMessageBox.information = lambda *a, **k: dialogs.append(f"info: {a[1]}: {a[2]}") or QMessageBox.StandardButton.Ok
QMessageBox.question = lambda *a, **k: dialogs.append(f"question: {a[1]}: {a[2]}") or QMessageBox.StandardButton.Yes
QMessageBox.about = lambda *a, **k: dialogs.append(f"about: {a[2]}")

archive = None
if not ARGS.live:
    from fake_archive import FakeArchive, make_posts

    NOW = time.time()
    archive = FakeArchive(make_posts(ARGS.small, NOW, days=40, per_day=(40, 25, 60), seed=1)
                          + make_posts(ARGS.busy, NOW, days=40, per_day=(700, 650, 900), seed=3),
                          rate=(4, 1.0), busy={ARGS.busy.lower()})
    archive.delay = ARGS.latency
    arctic_shift.BASE_URL = archive.url
    # Big real threads: the fake's trees hold up to num_comments, so give settled busy posts more.
    for p in archive.posts:
        if p["num_comments"]:
            p["num_comments"] *= 40

beat = {"last": time.perf_counter()}
doing = [""]  # what the walkthrough is doing now, so a freeze can be pinned on it
stall_where: list = []


def tick():
    now = time.perf_counter()
    gap = (now - beat["last"]) * 1000
    if gap > 200:
        stalls.append(gap)
        stall_where.append((round(gap), doing[0]))
    beat["last"] = now


heart = QTimer()
heart.setInterval(20)
heart.timeout.connect(tick)
heart.start()

if not QGuiApplication.platformName().startswith("offscreen"):
    # A CI desktop is smaller than the screen being tested: open the window at the size the app would pick there.
    fw, fh = int(pw / ARGS.scale * 0.94), int((ph / ARGS.scale - 79) * 0.92)
    mw.fit_to_screen = lambda size, screen: size.boundedTo(size.__class__(fw, fh))
print("opening the window", flush=True)
w = mw.MainWindow(DB)
F = getattr(w, "fetch_form", w)  # the fetch fields, on their own widget since #67
FORM_MOD = sys.modules[type(F).__module__] if F is not w else mw
w.show()
print("window shown", flush=True)
pump(0.5)
screen = QGuiApplication.primaryScreen()
info = {"live": ARGS.live, "dpr": w.devicePixelRatioF(), "screen_logical": [screen.geometry().width(),
                                                                           screen.geometry().height()],
        "window": [w.width(), w.height()], "latency": None if ARGS.live else ARGS.latency}


@step("first-launch: an empty app, restored and maximized")
def first_launch(e):
    e["shots"].append(shot(w, "first-launch-restored"))
    w.showMaximized()
    pump(0.5)
    e["shots"].append(shot(w, "first-launch-maximized"))
    e["notes"].append(f"dpr {w.devicePixelRatioF()}, window {w.width()}x{w.height()}, heading "
                      f"{w.heading.text()!r}, fetch enabled {w.fetch_btn.isEnabled()}")


@step("small-fetch: type a subreddit and press Fetch and analyse with the default form")
def small_fetch(e):
    e["form"] = {"range": F.range.currentText(), "pick": getattr(F, "pick", None) and F.pick.currentText(),
                 "limit": getattr(F, "limit", None) and F.limit.value(), "comments": F.include_comments.isChecked(), "per_post": F.comments_per_post.value()}
    open_drawer("fetch")
    type_into(F.subreddit, ARGS.small)
    run_fetch(e, w, ARGS.small, lambda: click(w.fetch_btn))


@step("busy-fetch: with the first subreddit on screen, fetch a busy one")
def busy_fetch(e):
    e["showing_before"] = w.heading.text()
    open_drawer("fetch")
    type_into(F.subreddit, ARGS.busy)
    run_fetch(e, w, ARGS.busy, lambda: click(w.fetch_btn))
    shown = {s["heading"] for s in e["samples"]}
    e["notes"].append(f"headings seen while fetching: {sorted(shown)}")


@step("stop: fetch more of the busy subreddit and press Stop partway")
def stop_midway(e):
    open_drawer("fetch")
    F.range.setCurrentIndex(list(FORM_MOD.RANGES).index("month"))
    if hasattr(F, "limit"):  # max posts went with #55 (every post in the range)
        F.limit.setValue(300)
    run_fetch(e, w, ARGS.busy, lambda: click(w.fetch_btn), stop_after=ARGS.stop_after)
    b, end = e.get("before_stop", {}), e["end"]
    e["notes"].append(f"at Stop: db {b.get('db_posts')}/{b.get('db_comments')}, window {b.get('posts_rows')}/"
                      f"{b.get('comments_rows')}; idle {e.get('stop_to_idle_secs')}s later: db "
                      f"{end['db_posts']}/{end['db_comments']}, window {end['posts_rows']}/{end['comments_rows']}")


@step("days: pick days in the header's Days picker on the first subreddit")
def days_pick(e):
    if not hasattr(w, "days_pick"):
        e["notes"].append("no Days picker on this main")
        return
    from subsentiment.gui import days as dd
    open_drawer("saved")
    for i in range(w.datasets.count()):
        if (w.datasets.item(i).data(Qt.ItemDataRole.UserRole) or "").lower() == ARGS.small.lower():
            w.datasets.setCurrentRow(i)
    pump(0.5)
    all_posts, all_comments = len(w.posts), len(w.comments)
    combo = w.days_pick
    e["notes"].append(f"{w.heading.text()}: {all_posts}/{all_comments} with {combo.currentText()!r}, "
                      f"{len(combo.counts)} saved days, picker enabled {combo.isEnabled()}")
    click(combo)
    pump(0.5)
    picker = combo.picker
    e["notes"].append(f"popup open {picker.isVisible()}")
    e["shots"].append(shot(w, "days-open"))
    if picker.isVisible():
        picker.grab().save(str(OUT / "days-popup.png"))
        e["shots"].append("days-popup.png")

    def check(label):
        pump(0.6)
        chosen = w.chosen_days
        want_p = len(dd.keep(w.store.posts(w.current), chosen))
        want_c = len(dd.keep(w.store.comments(w.current), chosen))
        shown = (w.posts_tab.view.model().rowCount(), w.comments_tab.view.model().rowCount())
        tiles = getattr(w, "tiles", None) or w.overview.tiles  # moved into OverviewPage by #62
        tile = tiles["count"].value.text() if hasattr(tiles["count"], "value") else ""
        ok = shown == (want_p, want_c)
        e["notes"].append(f"{label}: combo {combo.currentText()!r}, tables {shown[0]}/{shown[1]}, expected "
                          f"{want_p}/{want_c}{'' if ok else ' MISMATCH'}; items tile {tile}; tabs {tab_text('posts')!r} "
                          f"{tab_text('comments')!r}; subtitle {w.subheading.text()!r}")

    click(picker.none_btn)
    check("None")
    cal = picker.calendar
    day = max(combo.counts) if combo.counts else None
    rect = cal.rect_of(day) if day is not None else None
    if rect is not None:
        QTest.mouseClick(cal, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
        check(f"one day ({dd.as_date(day)})")
    e["shots"].append(shot(w, "days-one-day"))
    click(picker.week_btn)
    check("Last 7 days")
    click(picker.all_btn)
    check("All days")
    picker.hide()
    pump(0.3)
    # A narrow choice survives switching away and back, and export follows it.
    click(combo)
    pump(0.3)
    if rect is not None:
        click(picker.none_btn)
        QTest.mouseClick(cal, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, rect.center())
    picker.hide()
    pump(0.5)
    path = TMP / "days-posts.csv"
    QFileDialog.getSaveFileName = lambda *a, **k: (str(path), "")
    w.export_posts.trigger()
    pump(0.3)
    import csv

    rows = sum(1 for _ in csv.reader(path.open(encoding="utf-8-sig"))) - 1 if path.exists() else None
    e["notes"].append(f"export with one day chosen: {rows} rows, table shows {w.posts_tab.view.model().rowCount()}")
    e["shots"].append(shot(w, "days-chosen-closed"))
    click(combo)
    pump(0.3)
    click(picker.all_btn)
    picker.hide()
    pump(0.5)


@step("span: zoom the timeline and check every page counts the days it shows")
def span_check(e):
    if not hasattr(w, "span"):
        e["notes"].append("no timeline span on this main")
        return
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent
    from subsentiment.gui import days as dd

    open_drawer("saved")
    for i in range(w.datasets.count()):
        if (w.datasets.item(i).data(Qt.ItemDataRole.UserRole) or "").lower() == ARGS.small.lower():
            w.datasets.setCurrentRow(i)
    open_drawer("")
    go("overview")
    pump(0.8)
    t = w.overview.timeline

    def check(label):
        pump(0.8)  # the tables follow once the span settles (300 ms)
        rows_p, rows_c = w.store.posts(w.current), w.store.comments(w.current)
        want = (len(dd.keep(rows_p, w._table_days)), len(dd.keep(rows_c, w._table_days)))
        shown = (w.posts_tab.view.model().rowCount(), w.comments_tab.view.model().rowCount())
        tile = w.overview.tiles["count"].value.text()
        ok = shown == want and tile.replace(",", "") == str(sum(want))
        e["notes"].append(f"{label}: span {w._table_days and dd.span_text(w._table_days)!r}, tables {shown[0]}/"
                          f"{shown[1]}, expected {want[0]}/{want[1]}, items tile {tile}{'' if ok else ' MISMATCH'}; "
                          f"subtitle {w.subheading.text()!r}")
        return ok

    click(t.presets["All"])
    check("All")
    plot = t._areas()[0]
    mid = plot.center()
    for _ in range(4):
        QApplication.sendEvent(t, QWheelEvent(QPointF(mid), QPointF(t.mapToGlobal(mid.toPoint())), QPoint(0, 0),
                                              QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                                              Qt.ScrollPhase.NoScrollPhase, False))
        pump(0.1)
    check("4 wheel notches in")
    e["shots"].append(shot(w, "span-zoomed-overview"))
    go("posts")
    e["shots"].append(shot(w, "span-zoomed-posts"))
    path = TMP / "span-posts.csv"
    QFileDialog.getSaveFileName = lambda *a, **k: (str(path), "")
    w.export_posts.trigger()
    pump(0.3)
    import csv

    rows = sum(1 for _ in csv.reader(path.open(encoding="utf-8-sig"))) - 1 if path.exists() else None
    e["notes"].append(f"export while zoomed: {rows} rows, table shows {w.posts_tab.view.model().rowCount()}")
    go("overview")
    QTest.mouseDClick(t, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, mid.toPoint())
    check("double-click back to every day")


@step("words and score: the word cloud, and the score filter on every page")
def words_and_score(e):
    if "words" not in pages() or not hasattr(w.words_page.body, "placed"):
        e["notes"].append("no word cloud on this main")
    else:
        t0 = time.perf_counter()
        go("words")
        pump(1.5)  # counted off the GUI thread
        cloud = w.words_page.body
        placed = cloud.placed()
        e["notes"].append(f"word cloud: {len(cloud._words)} words counted, {len(placed)} drawn, top "
                          f"{[p[1] for p in placed[:5]]}, shown in {time.perf_counter() - t0:.1f}s")
        e["shots"].append(shot(w, "words"))
    if not hasattr(w, "score_pick"):
        return
    from subsentiment.store import scoring_at_least

    go("posts")
    for text in ("Score 100+", "Any score"):
        w.score_pick.setCurrentIndex(w.score_pick.findText(text))
        pump(0.8)
        want = len(scoring_at_least(w.store.posts(w.current), w.min_score))
        shown = w.posts_tab.view.model().rowCount()
        e["notes"].append(f"{text}: posts table {shown}, expected {want}{'' if shown == want else ' MISMATCH'}; "
                          f"subtitle {w.subheading.text()!r}")
        if text != "Any score":
            e["shots"].append(shot(w, "score-100"))


@step("look-around: every tab, scope, theme and View option")
def look_around(e):
    for name in pages():
        doing[0] = f"page {name}"
        go(name)
        e["shots"].append(shot(w, f"page-{name}"))
    go("overview")
    for name in ("fetch", "saved") if hasattr(w, "rail") else ():
        doing[0] = f"drawer {name}"
        open_drawer(name)
        e["shots"].append(shot(w, f"drawer-{name}"))
    open_drawer("")
    for i in range(w.scope.count()):
        w.scope.setCurrentIndex(i)
        pump(0.2)
    doing[0] = "theme light"
    w._set_theme("light")
    e["shots"].append(shot(w, "overview-light"))
    doing[0] = "reduce transparency"
    w.reduce_act.setChecked(True)
    e["shots"].append(shot(w, "reduce-transparency"))
    w.reduce_act.setChecked(False)
    w.mood_act.setChecked(False)
    w.mood_act.setChecked(True)
    doing[0] = "theme dark"
    w._set_theme("dark")
    pump(0.5)  # a person doesn't restore the window in the same instant
    doing[0] = "restore"
    w.showNormal()
    pump(0.5)
    e["shots"].append(shot(w, "restored-dark"))
    w.showMaximized()
    pump(0.3)
    w._show_about()
    e["notes"].append(f"about box: {dialogs[-1][:160] if dialogs else ''}")


@step("motion: fades with motion on, none with View > Reduce motion")
def motion_check(e):
    from PySide6.QtWidgets import QLabel

    def fades():
        return [c for c in w.findChildren(QLabel) if c.objectName() == "motion-snapshot"]

    def try_it(label):
        out = {}
        for what, act in (("page", lambda: go(pages()[(pages().index(current_page()) + 1) % len(pages())])),
                          ("theme", lambda: w._set_theme("light" if ARGS.theme == "dark" else "dark")),
                          ("theme back", lambda: w._set_theme(ARGS.theme))):
            t = time.perf_counter()
            act()
            took = time.perf_counter() - t
            started = len(fades())
            pump(0.1)
            if what == "theme" and label == "on":
                path = OUT / f"{len(list(OUT.glob('*.png'))):02d}-motion-theme-mid-fade.png"
                w.grab().save(str(path))
                e["shots"].append(path.name)
            pump(1.0)
            left = fades()
            seen = [f for f in left if f.isVisibleTo(w) and f.parentWidget().isVisibleTo(w)]
            out[what] = (f"{started} fading, {len(left)} left after 1.1s ({len(seen)} visible: "
                         f"{[f.parentWidget().objectName() or type(f.parentWidget()).__name__ for f in seen]}), "
                         f"call {took * 1000:.0f} ms")
        e["notes"].append(f"motion {label}: " + "; ".join(f"{k} {v}" for k, v in out.items()))

    e["notes"].append(f"Reduce motion starts {'on' if w.motion_act.isChecked() else 'off'}")
    try_it("on")
    w.motion_act.setChecked(True)
    pump(0.2)
    try_it("off (Reduce motion checked)")
    w.motion_act.setChecked(False)
    pump(0.2)
    e["notes"].append(f"setting saved as {QSettings().value('ui/reduce_motion')!r} after unchecking")


@step("timeline: range buttons, wheel zoom, drag and double-click on the Overview's timeline")
def timeline(e):
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    t = timeline_chart()
    go("timeline" if "timeline" in pages() else "overview")
    pump(0.5)
    if not hasattr(t, "presets"):
        e["notes"].append("no zoomable timeline on this main")
        return

    def span(label):
        lo, hi = t.view or t.domain()
        e["notes"].append(f"{label}: {(hi - lo) / 86400:.1f} days in view, {len(t.visible())} points, "
                          f"checked {[k for k, b in t.presets.items() if b.isChecked()]}")

    span("start")
    click(t.presets["1w"])
    pump(0.8)
    span("1w button")
    e["shots"].append(shot(w, "timeline-1w"))
    click(t.presets["All"])
    pump(0.8)
    span("All button")
    plot = t._areas()[0]
    mid = plot.center()
    for _ in range(3):
        ev = QWheelEvent(QPointF(mid), QPointF(t.mapToGlobal(mid.toPoint())), QPoint(0, 0), QPoint(0, 120),
                         Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
        QApplication.sendEvent(t, ev)
        pump(0.15)
    pump(0.6)
    span("3 wheel notches up")
    e["shots"].append(shot(w, "timeline-wheel"))
    a, b = QPoint(int(plot.left() + plot.width() * 0.2), int(mid.y())), QPoint(int(plot.left() + plot.width() * 0.5), int(mid.y()))
    QTest.mousePress(t, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, a)
    for k in range(1, 11):
        QTest.mouseMove(t, a + (b - a) * (k / 10))
        pump(0.02)
    QTest.mouseRelease(t, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, b)
    pump(0.8)
    span("drag across a third of the plot")
    QTest.mouseDClick(t, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, mid.toPoint())
    pump(0.8)
    span("double-click")


@step("export: File > Export posts and comments as CSV")
def export(e):
    for kind, act, view in (("posts", w.export_posts, w.posts_tab), ("comments", w.export_comments, w.comments_tab)):
        path = TMP / f"{kind}.csv"
        QFileDialog.getSaveFileName = lambda *a, p=str(path), **k: (p, "")
        t = time.perf_counter()
        act.trigger()
        pump(0.3)
        import csv

        rows = sum(1 for _ in csv.reader(path.open(encoding="utf-8-sig"))) - 1 if path.exists() else None
        e["notes"].append(f"{kind}: {rows} rows written in {time.perf_counter() - t:.1f}s, window shows "
                          f"{view.view.model().rowCount()}")


@step("comments-all: Comments per post All on the small subreddit's past day")
def comments_all(e):
    import sqlite3

    open_drawer("fetch")
    F.range.setCurrentIndex(list(FORM_MOD.RANGES).index("day"))
    F.include_comments.setChecked(True)
    F.comments_per_post.setValue(F.comments_per_post.minimum())
    e["per_post_text"] = F.comments_per_post.text()
    type_into(F.subreddit, ARGS.small)
    run_fetch(e, w, ARGS.small, lambda: click(w.fetch_btn), shots_at=(5, 30))
    con = sqlite3.connect(DB)
    rows = con.execute("select p.num_comments, count(c.id) from posts p left join comments c on c.post_id = p.id "
                       "where lower(p.subreddit) = ? group by p.id", (ARGS.small.lower(),)).fetchall()
    con.close()
    full = sum(1 for n, got in rows if got >= (n or 0))
    e["notes"].append(f"spin box reads {e['per_post_text']!r}; {full} of {len(rows)} posts have every comment "
                      f"num_comments counts; most on one post {max((g for _, g in rows), default=0)}")
    F.comments_per_post.setValue(FORM_MOD.FETCH_DEFAULTS["comments_per_post"])


@step("coverage: open View > Download coverage for the busy subreddit")
def coverage_open(e):
    open_drawer("saved")
    for i in range(w.datasets.count()):
        if (w.datasets.item(i).data(Qt.ItemDataRole.UserRole) or "").lower() == ARGS.busy.lower():
            w.datasets.setCurrentRow(i)
    pump(0.5)
    open_drawer("")
    go("coverage") if "coverage" in pages() else w.coverage_act.trigger()  # a rail page since #91
    pump(1)
    c = w.coverage
    e["shots"].append(shot(c, "coverage-open"))
    t0 = time.monotonic()
    while busy(c, "count_job", "count_thread") and time.monotonic() - t0 < 120:
        pump(0.2)
    e["count_secs"] = round(time.monotonic() - t0, 1)
    pump(0.5)
    e["shots"].append(shot(c, "coverage-counted"))
    e["notes"].append(f"counts took {e['count_secs']}s; status {c.status.text()!r}; subtitle {c.subtitle.text()!r}")


@step("coverage-day: Download all on yesterday")
def coverage_day(e):
    c = w.coverage
    row = 1 if c.day_table.rowCount() > 1 else 0
    c.day_table.selectRow(row)
    button = c.day_table.cellWidget(row, 2).findChild(QPushButton)
    e["button"] = button.text()
    run_fetch(e, w, ARGS.busy, lambda: click(button), shots_at=(5, 30))
    e["shots"].append(shot(c, "coverage-day-done"))
    e["notes"].append(f"coverage status {c.status.text()!r}, posts title {c.posts_title.text()!r}")


@step("coverage-comments: Download all comments for that day")
def coverage_comments(e):
    c = w.coverage
    e["button"] = c.all_comments.text()
    run_fetch(e, w, ARGS.busy, lambda: click(c.all_comments), stop_after=ARGS.timeout * 0.5, shots_at=(5, 30))
    e["shots"].append(shot(c, "coverage-comments-done"))
    e["notes"].append(f"coverage status {c.status.text()!r}")


@step("delete: File > Delete saved data for the busy subreddit")
def delete(e):
    if hasattr(w, "rail") and "coverage" in pages():
        go("overview")  # a page since #91, not a window to close
    elif w.coverage is not None:
        w.coverage.close()
    t = time.perf_counter()
    w.delete_act.trigger()
    pump(0.5)
    p, c = db_counts(ARGS.busy)
    e["notes"].append(f"left in db {p}/{c} after {time.perf_counter() - t:.1f}s; heading {w.heading.text()!r}; "
                      f"saved list {[w.datasets.item(i).text() for i in range(w.datasets.count())]}")
    e["shots"].append(shot(w, "after-delete"))


for fn in (first_launch, small_fetch, busy_fetch, stop_midway, days_pick, span_check, words_and_score, look_around, motion_check, timeline, export, comments_all, coverage_open, coverage_day,
           coverage_comments, delete):
    fn()

summary = {"workarounds": WORKAROUNDS, "info": info, "steps": log, "dialogs": dialogs, "http": {**http, "status": dict(http["status"])},
           "local": local, "stalls_ms": sorted((round(s) for s in stalls), reverse=True)[:20],
           "worst_stalls_where": sorted(stall_where, reverse=True)[:12]}
if archive is not None:
    summary["archive_violations"] = archive.violations()[:20]
(OUT / "walkthrough.json").write_text(json.dumps(summary, indent=1, default=str))
lines = [f"# Walkthrough ({'live Arctic Shift' if ARGS.live else 'fake archive'}, dpr {info['dpr']})", ""]
for s in log:
    lines += [f"## {s['step']} ({s['secs']} s)", ""] + [f"- {n}" for n in s["notes"]]
    if s.get("error"):
        lines += ["", "```", s["error"].strip(), "```"]
    c = s["cost"]
    lines += [f"- requests {c['http']} ({c['http_secs']} s on the wire) {c['status']}; commits {c['commits']} "
              f"({c['commit_secs']} s); sentiment calls {c['scores']} ({c['score_secs']} s); window froze "
              f"{c['stalls']} times, worst {c['worst_stall_ms']} ms", ""]
lines += ["## Dialogs", ""] + [f"- {d[:200]}" for d in dialogs]
(OUT / "walkthrough.md").write_text("\n".join(lines) + "\n")
print(f"wrote {OUT / 'walkthrough.md'}")
w.close()
if archive is not None:
    archive.close()
os._exit(0)
