"""Drive the real ikari window headless: commands on stdin, screenshots to a folder.

    printf 'fetch python week\nss overview\nquit\n' | python .claude/skills/run-ikari/driver.py

The real MainWindow, fetch threads, HTTP client and store, talking to tests/fake_archive.py on localhost (never
the real Arctic Shift), with a throwaway database and settings. One command per line; # starts a comment. Run it
under tmux for an interactive session. Commands:

    fetch SUB [day|week|month] [comments|nocomments]   fetch from the Fetch drawer and wait until it's done
    page overview|words|posts|comments|coverage         switch rail page (coverage asks the archive for day counts)
    theme light|dark      size W H      max      restore
    click PATH            mouse-click a widget found from the window, e.g. rail.buttons.fetch or fetch_btn
    wait MS               keep Qt running for MS milliseconds
    ss NAME               save the window to OUT/NAME.png (pixels = logical size x scale)
    eval EXPR             print repr(EXPR); names: w (window), app, archive, QTest, Qt, pump
    exec STATEMENT        run a Python statement with the same names
    requests              what the fake archive was asked, by path
    quit
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shlex
import sys
import tempfile
import threading
import time
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT / "tools")]

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--out", default=str(ROOT / ".claude/skills/run-ikari/shots"), help="folder for screenshots")
parser.add_argument("--db", help="open this database (a copy is made; default: a new empty one)")
parser.add_argument("--sample", action="store_true", help="seed r/riverton with the README screenshots' data")
parser.add_argument("--screen", default="1920x1200", help="the fake screen in pixels (the owner's)")
parser.add_argument("--scale", type=float, default=1.5, help="display scaling (1.5 is the owner's 150%%)")
parser.add_argument("--size", help="logical window size, e.g. 1280x800 (default: the app's own opening size)")
parser.add_argument("--theme", default="dark", choices=["dark", "light"], help="starting theme")
parser.add_argument("--real-speed", action="store_true", help="the app's own request pacing (10x slower)")
args = parser.parse_args()
OUT = Path(args.out).resolve()
DB = Path(args.db).resolve() if args.db else None  # before the chdir below
TMP = Path(tempfile.mkdtemp(prefix="ikari-driver-"))
# A real screen for the offscreen platform (its default is 800x800, so Maximize gave a tiny window), scaled the
# way Windows' display setting does it: logical DPI over base DPI. Named from its own folder, as in
# tests/test_responsive.py: Qt splits the platform's options at colons.
width, height = map(int, args.screen.split("x"))
(TMP / "screen.json").write_text(json.dumps({"screens": [{"name": "s", "x": 0, "y": 0, "width": width, "height": height,
                                                          "logicalDpi": 96 * args.scale, "logicalBaseDpi": 96, "dpr": 1}]}))
os.chdir(TMP)
os.environ["QT_QPA_PLATFORM"] = "offscreen:configfile=screen.json"
os.environ.pop("QT_SCALE_FACTOR", None)

from PySide6.QtCore import QSettings, Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from fake_archive import Clock, FakeArchive, make_posts  # noqa: E402
from subsentiment import arctic_shift  # noqa: E402
from subsentiment.gui import motion, theme, worker  # noqa: E402
from subsentiment.gui.main_window import MainWindow  # noqa: E402


def pump(ms: int) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)


class Driver:
    def __init__(self):
        Clock.pin()  # noon UTC today, as the tests run: the fake archive's days line up with the app's ranges
        now = time.time()
        self.tmp, self.out = TMP, OUT
        self.out.mkdir(parents=True, exist_ok=True)
        db = self.tmp / "driver.db"
        if DB:
            db.write_bytes(DB.read_bytes())
        elif args.sample:
            import screenshot
            screenshot.seed(str(db))
        self.archive = FakeArchive(make_posts("python", now, days=35) + make_posts("rust", now, per_day=(3,), seed=2)
                                   + make_posts("AskReddit", now, days=3, per_day=(150,), seed=3), busy={"AskReddit"})
        arctic_shift.BASE_URL = self.archive.url
        if not args.real_speed:  # as tests/test_app_e2e.py: ten times the app's pace, waits capped at 1.2 s
            arctic_shift.MAX_WAIT = 1.2
            worker.LIMITER = arctic_shift.RateLimiter(arctic_shift.BASE_INTERVAL / 10)
            self.archive.rate = (30, 1.0)
        for name in ("warning", "information", "critical"):  # a modal box would block the driver forever
            setattr(QMessageBox, name, staticmethod(lambda *a, _n=name, **k: print(f"[{_n}]", *a[1:3])))
        QMessageBox.question = staticmethod(lambda *a, **k: print("[question]", *a[1:3], "-> Yes")
                                            or QMessageBox.StandardButton.Yes)

        self.app = QApplication(sys.argv[:1])
        self.app.setOrganizationName("ikari-driver")  # never the owner's settings or data folder
        self.app.setApplicationName(self.tmp.name)
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(self.tmp / "cfg"))
        QSettings().setValue("updates/auto", False)
        QSettings().setValue("ui/theme", args.theme)
        theme.load_fonts(self.app)
        motion.set_enabled(False)  # screenshots of finished states, not mid-fade
        self.w = MainWindow(str(db))
        if args.size:
            self.w.resize(*map(int, args.size.split("x")))
        self.w._apply_theme()  # the style sheet; without it the window is plain unstyled Qt
        self.w.show()
        pump(300)
        self.names = {"w": self.w, "app": self.app, "archive": self.archive, "QTest": QTest, "Qt": Qt, "pump": pump}
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.running = False
        threading.Thread(target=self._read_stdin, daemon=True).start()
        self.timer = QTimer(interval=20, timeout=self._next)
        self.timer.start()
        print(f"ready: db {db}, archive {self.archive.url}, shots in {self.out}", flush=True)

    def _read_stdin(self) -> None:
        for line in sys.stdin:
            self.lines.put(line)
        self.lines.put(None)  # end of input quits

    def _next(self) -> None:
        if self.running:  # a command is pumping events; don't start another inside it
            return
        try:
            line = self.lines.get_nowait()
        except queue.Empty:
            return
        if line is None:
            return self.quit()
        line = line.split("#", 1)[0].strip() if not line.lstrip().startswith(("eval", "exec")) else line.strip()
        if not line:
            return
        self.running = True
        try:
            cmd, _, rest = line.partition(" ")
            getattr(self, "do_" + cmd, self.unknown)(rest.strip())
        except Exception:
            traceback.print_exc()
        finally:
            self.running = False
            print("ok" if cmd != "quit" else "", flush=True)

    def unknown(self, rest: str) -> None:
        print("unknown command; see the top of driver.py")

    def wait_idle(self, timeout: float = 120) -> None:
        end = time.monotonic() + timeout
        while self.w.fetch_job.busy or self.w.coverage.reading():
            if time.monotonic() > end:
                raise TimeoutError("still busy after %d s" % timeout)
            pump(20)
        pump(300)

    def do_fetch(self, rest: str) -> None:
        sub, *opts = shlex.split(rest)
        w = self.w
        if w._drawer_key() != "fetch":
            QTest.mouseClick(w.rail.buttons["fetch"], Qt.MouseButton.LeftButton)
        w.fetch_form.subreddit.setText(sub)
        w.fetch_form.include_comments.setChecked("nocomments" not in opts)
        rng = next((o for o in opts if o in ("day", "week", "month")), "week")
        w.fetch_form.range.setCurrentIndex(w.fetch_form.range.findData(rng))
        QTest.mouseClick(w.fetch_btn, Qt.MouseButton.LeftButton)
        start = time.monotonic()
        self.wait_idle()
        print(f"fetched in {time.monotonic() - start:.1f} s: {w.progress_msg.text()}")

    def do_page(self, rest: str) -> None:
        self.w.show_page(rest)
        pump(800)  # charts, word tallies; coverage reads on threads
        self.wait_idle()

    def do_theme(self, rest: str) -> None:
        QSettings().setValue("ui/theme", rest)
        self.w._apply_theme()
        pump(300)

    def do_size(self, rest: str) -> None:
        self.w.showNormal()
        self.w.resize(*map(int, rest.split()))
        pump(300)

    def do_max(self, rest: str) -> None:
        self.w.showMaximized()
        pump(300)

    def do_restore(self, rest: str) -> None:
        self.w.showNormal()
        pump(300)

    def do_click(self, rest: str) -> None:
        target = self.w
        for part in rest.split("."):
            target = target[part] if isinstance(target, dict) else getattr(target, part)
        QTest.mouseClick(target, Qt.MouseButton.LeftButton)
        pump(300)

    def do_wait(self, rest: str) -> None:
        pump(int(rest or 500))

    def do_ss(self, rest: str) -> None:
        path = self.out / f"{rest or 'shot'}.png"
        image = self.w.grab()
        image.save(str(path))
        print(f"saved {path} ({image.width()}x{image.height()} px at x{image.devicePixelRatio()})")

    def do_eval(self, rest: str) -> None:
        print(repr(eval(rest, self.names)))

    def do_exec(self, rest: str) -> None:
        exec(rest, self.names)

    def do_requests(self, rest: str) -> None:
        print(dict(Counter(self.archive.paths())))

    def do_quit(self, rest: str = "") -> None:
        self.quit()

    def quit(self) -> None:
        self.timer.stop()
        self.w.close()
        self.archive.close()
        self.app.quit()


if __name__ == "__main__":
    driver = Driver()
    driver.app.exec()
    # A fetch thread still out would make Qt abort on the way out; everything is in the throwaway folder anyway.
    os._exit(0)
