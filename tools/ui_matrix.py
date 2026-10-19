"""Render the main window on common screens and scalings, and flag layout problems.

    python tools/ui_matrix.py --out shots/            # every screen, restored + maximized + a resize sweep
    python tools/ui_matrix.py --only win-1920x1200    # one screen, every scaling listed for it
    python tools/ui_matrix.py --only 150 --theme light

Each screen runs in its own Qt process on the offscreen platform, configured with that screen's size and
device pixel ratio, and with the font DPI the real OS uses (Windows 96, macOS 72, which makes the same
point size smaller on a Mac). The taskbar, Dock, menu bar and title bar are taken off the free area by
hand, since the offscreen platform has none. Restored is the size the app opens at; maximized is the free
area; the sweep drags the window from maximized down to its minimum size in a few steps.

Checks on every size: the window can't shrink to fit the screen, a label's text is cut off, a field or
button is squashed below what its text needs, a widget sticks out of its parent, and which scroll bars
show (some are on purpose). Writes one PNG per size and a report.md. Uses the same made-up sample data as
tools/screenshot.py; your own data and settings are not touched.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Logical (point) sizes the taskbar and title bars take up.
WIN_TASKBAR, WIN_CAPTION = 48, 31  # Windows 11
MAC_MENU, MAC_MENU_NOTCH, MAC_TITLE, MAC_DOCK = 25, 37, 28, 70  # Dock shown at the bottom, default size

# (id, os, physical width, physical height, scalings). Windows: scaling is the display setting.
# macOS: Retina panels at their default "looks like" size are 2x; plain 1080p/1440p monitors run at 1x.
SCREENS = [
    ("win-1280x800", "win", 1280, 800, [100, 125]),
    ("win-1366x768", "win", 1366, 768, [100, 125]),
    ("win-1440x900", "win", 1440, 900, [100, 125]),
    ("win-1536x864", "win", 1536, 864, [100, 125]),
    ("win-1600x900", "win", 1600, 900, [100, 125]),
    ("win-1680x1050", "win", 1680, 1050, [100, 125]),
    ("win-1920x1080", "win", 1920, 1080, [100, 125, 150]),
    ("win-1920x1200", "win", 1920, 1200, [100, 125, 150]),
    ("win-2560x1440", "win", 2560, 1440, [100, 125, 150, 175]),
    ("win-2560x1600", "win", 2560, 1600, [100, 125, 150, 175, 200]),
    ("win-2880x1800", "win", 2880, 1800, [150, 175, 200]),
    ("win-3840x2160", "win", 3840, 2160, [150, 175, 200, 225]),
    ("win-3840x2400", "win", 3840, 2400, [200, 225, 250]),
    ("mac-1280x800", "mac", 2560, 1600, [200]),   # 13" MacBook Air/Pro (Intel)
    ("mac-1440x900", "mac", 2880, 1800, [200]),   # 13" MacBook Air M1, 15" Pro
    ("mac-1470x956", "mac", 2940, 1912, [200]),   # 13" MacBook Air M2+ (notch)
    ("mac-1512x982", "mac", 3024, 1964, [200]),   # 14" MacBook Pro (notch)
    ("mac-1710x1112", "mac", 3420, 2224, [200]),  # 15" MacBook Air (notch)
    ("mac-1728x1117", "mac", 3456, 2234, [200]),  # 16" MacBook Pro (notch)
    ("mac-1920x1080", "mac", 1920, 1080, [100]),  # 1080p monitor
    ("mac-2560x1440", "mac", 2560, 1440, [100]),  # 1440p monitor
    ("mac-2560x1440r", "mac", 5120, 2880, [200]),  # 27" Studio Display / 5K iMac
]
NOTCHED = {"mac-1470x956", "mac-1512x982", "mac-1710x1112", "mac-1728x1117"}


def free_area(screen: str, os_name: str, w: float, h: float) -> tuple[int, int]:
    """Logical size a maximized (Windows) or zoomed (macOS) window's contents get."""
    if os_name == "win":
        return int(w), int(h - WIN_TASKBAR - WIN_CAPTION)
    menu = MAC_MENU_NOTCH if screen in NOTCHED else MAC_MENU
    return int(w), int(h - menu - MAC_TITLE - MAC_DOCK)


# -- child: one screen, one scaling ------------------------------------------------------------


def check(window) -> list[str]:
    """Layout problems visible in the window right now."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QAbstractButton, QAbstractScrollArea, QAbstractSpinBox, QComboBox, QLabel, QLineEdit, QScrollArea,
        QTabBar, QWidget,
    )

    def name(w: QWidget) -> str:
        bits = [type(w).__name__]
        text = getattr(w, "text", None)
        if callable(text) and isinstance(text(), str) and text():
            bits.append(repr(text()[:40]))
        elif w.objectName():
            bits.append(w.objectName())
        return " ".join(bits)

    def scrolled(w: QWidget) -> bool:
        """Inside a scroll area's contents, where being out of view is fine."""
        p = w.parentWidget()
        while p is not None and p is not window:
            if isinstance(p, QScrollArea) or (isinstance(p, QAbstractScrollArea) and w is not p):
                return True
            p = p.parentWidget()
        return False

    problems = []
    for w in window.findChildren(QWidget):
        if not w.isVisible() or w.window() is not window or w.width() <= 0:
            continue
        if isinstance(w, QLabel) and w.text() and not w.pixmap() and "<" not in w.text():
            m = w.fontMetrics()
            room = w.contentsRect()
            if w.wordWrap():
                need = w.heightForWidth(w.width())
                if need > w.height() + 1:
                    problems.append(f"cut off (wrapped text needs {need}px, has {w.height()}): {name(w)}")
            elif m.horizontalAdvance(w.text()) > room.width() + 1:
                problems.append(f"cut off ({m.horizontalAdvance(w.text())}px text in {room.width()}): {name(w)}")
        if isinstance(w, (QAbstractButton, QComboBox, QLineEdit, QAbstractSpinBox, QTabBar)):
            hint = w.minimumSizeHint().boundedTo(w.maximumSize())  # a style cap is on purpose
            if w.width() + 1 < hint.width() or w.height() + 1 < hint.height():
                problems.append(f"squashed ({w.width()}x{w.height()}, needs {hint.width()}x{hint.height()}): "
                                f"{name(w)}")
        parent = w.parentWidget()
        if parent is not None and parent.isVisible() and not scrolled(w) and not w.isWindow():
            g = w.geometry()
            r = parent.rect()
            if g.right() > r.right() + 1 or g.bottom() > r.bottom() + 1 or g.left() < -1 or g.top() < -1:
                problems.append(f"sticks out of {type(parent).__name__} ({g.x()},{g.y()} {g.width()}x{g.height()} "
                                f"in {r.width()}x{r.height()}): {name(w)}")
    for area in window.findChildren(QScrollArea):
        inner = area.widget()
        sideways_off = area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        if area.isVisible() and inner is not None and sideways_off and inner.width() > area.viewport().width() + 1:
            problems.append(f"cut off sideways ({inner.width()}px wide in {area.viewport().width()}): "
                            f"scroll area of {type(inner.layout()).__name__ if inner.layout() else 'widget'}")
    return problems


def scrollbars(window) -> list[str]:
    from PySide6.QtWidgets import QAbstractScrollArea

    shown = []
    for area in window.findChildren(QAbstractScrollArea):
        if not area.isVisible():
            continue
        for bar, axis in ((area.verticalScrollBar(), "vertical"), (area.horizontalScrollBar(), "sideways")):
            if bar.isVisible() and bar.maximum() > 0:
                shown.append(f"{axis} on {type(area).__name__} {area.objectName() or ''}".rstrip())
    return shown


def child(spec: dict) -> int:
    from PySide6.QtCore import QSettings, QSize
    from PySide6.QtWidgets import QApplication

    from subsentiment.gui import main_window, native, theme

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import screenshot  # noqa: E402  (seeds the sample data)

    out = Path(spec["out"])
    native.setup_app_identity()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ikari-screenshots")
    app.setApplicationName("ikari-screenshots")
    app.setApplicationDisplayName("ikari")
    settings = QSettings()
    settings.setValue("updates/auto", False)
    settings.setValue("ui/theme", spec["theme"])
    settings.setValue("ui/tab", 0)
    settings.setValue("ui/last_dataset", screenshot.SUBREDDIT)
    if spec.get("subpixel"):  # the old look, for a before/after of the text rendering
        theme.smooth_text = lambda f: f
    theme.load_fonts(app)
    theme.apply(app, spec["theme"])  # before the window, as the app does, so first sizes are the styled ones

    window = main_window.MainWindow(spec["db"])
    window.subreddit.setText(screenshot.SUBREDDIT)
    window.tabbar.setFocus()

    class Free:  # the screen's free area, for the app's own opening-size rule
        def __init__(self, w, h):
            from PySide6.QtCore import QRect
            self.rect = QRect(0, 0, w, h + (WIN_CAPTION if spec["os"] == "win" else MAC_TITLE))

        def availableGeometry(self):
            return self.rect

    fw, fh = spec["free"]
    restored = main_window.fit_to_screen(QSize(1320, 860), Free(fw, fh))
    window.resize(restored)
    window.show()
    minimum = window.minimumSizeHint().expandedTo(window.minimumSize())

    sizes = [("restored", restored.width(), restored.height()), ("maximized", fw, fh)]
    if spec["sweep"]:
        for i in range(1, 5):  # maximized down to the minimum, both ways, then each way alone
            f = i / 4
            sizes.append((f"sweep-{i}", round(fw - (fw - minimum.width()) * f),
                          round(fh - (fh - minimum.height()) * f)))
        sizes.append(("narrow", minimum.width(), fh))
        sizes.append(("short", fw, minimum.height()))

    results = []
    for state, w, h in sizes:
        tabs = [0, 1] if state in ("restored", "maximized") else [0]
        for tab in tabs:
            window.tabbar.setCurrentIndex(tab)
            window.resize(w, h)
            for _ in range(6):
                app.processEvents()
                time.sleep(0.03)
            got = window.size()
            assert abs(window.devicePixelRatio() - spec["dpr"]) < 0.01, window.devicePixelRatio()
            problems = []
            if got.width() > fw or got.height() > fh:
                problems.append(f"window can't shrink to fit: {got.width()}x{got.height()} on a {fw}x{fh} free area")
            problems += check(window)
            shot = f"{spec['tag']}-{state}{'-posts' if tab else ''}.png"
            window.grab().save(str(out / shot))
            results.append({"state": state + (" (Posts tab)" if tab else ""), "size": [got.width(), got.height()],
                            "asked": [w, h], "problems": problems, "scrollbars": scrollbars(window), "shot": shot})
    print(json.dumps({"tag": spec["tag"], "minimum": [minimum.width(), minimum.height()], "results": results}))
    window.close()
    return 0


# -- parent: the whole matrix ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="ui-matrix", help="folder for the PNGs and report.md")
    parser.add_argument("--only", action="append", default=[], help="keep screens/scalings whose tag has this text")
    parser.add_argument("--theme", default="dark", choices=["dark", "light"])
    parser.add_argument("--no-sweep", action="store_true", help="skip the resize sweep")
    parser.add_argument("--subpixel", action="store_true",
                        help="render text with ClearType's colored edges, as before (Windows only differs)")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        return child(json.loads(args.child))

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import screenshot

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="ikari-matrix-"))
    db = tmp / "sample.db"
    screenshot.seed(str(db))

    reports = []
    for screen, os_name, pw, ph, scales in SCREENS:
        for scale in scales:
            tag = f"{screen}-{scale}"
            if args.only and not any(o in tag for o in args.only):
                continue
            dpr = scale / 100
            lw, lh = pw / dpr, ph / dpr
            dpi = 96 if os_name == "win" else 72
            config = tmp / f"{tag}.json"
            # Qt takes the scaling from logical DPI over base DPI, as it does from Windows' display setting.
            config.write_text(json.dumps({"screens": [{
                "name": tag, "x": 0, "y": 0, "width": pw, "height": ph,
                "logicalDpi": dpi * dpr, "logicalBaseDpi": dpi, "dpr": 1}]}))
            spec = {"tag": tag, "os": os_name, "dpr": dpr, "free": free_area(screen, os_name, lw, lh), "db": str(db),
                    "out": str(out), "theme": args.theme, "sweep": not args.no_sweep, "subpixel": args.subpixel}
            # A bare file name, run from its folder: a Windows drive colon would end the platform name early.
            env = dict(os.environ, QT_QPA_PLATFORM=f"offscreen:configfile={config.name}")
            env.pop("QT_SCALE_FACTOR", None)
            proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", json.dumps(spec)],
                                  env=env, cwd=tmp, capture_output=True, text=True, check=False)
            line = proc.stdout.strip().splitlines()[-1:] if proc.returncode == 0 else []
            if not line:
                print(f"{tag}: failed\n{proc.stderr[-2000:]}", file=sys.stderr)
                continue
            report = json.loads(line[0])
            report["logical"] = [round(lw), round(lh)]
            reports.append(report)
            bad = sum(bool(r["problems"]) for r in report["results"])
            print(f"{tag:24} logical {round(lw)}x{round(lh)}  min {report['minimum']}  "
                  f"{'OK' if not bad else f'{bad} sizes with problems'}")

    lines = ["# ikari layout matrix", "",
             f"Theme {args.theme}. Sizes are the window's contents in logical pixels.", "",
             "| Screen | Logical | Minimum | Size | Window | Problems | Scroll bars |", "|---|---|---|---|---|---|---|"]
    for rep in reports:
        for r in rep["results"]:
            lines.append(f"| {rep['tag']} | {rep['logical'][0]}x{rep['logical'][1]} | "
                         f"{rep['minimum'][0]}x{rep['minimum'][1]} | {r['state']} | {r['size'][0]}x{r['size'][1]} | "
                         f"{'<br>'.join(r['problems']) or 'none'} | {', '.join(r['scrollbars']) or 'none'} |")
    (out / "report.md").write_text("\n".join(lines) + "\n")
    (out / "report.json").write_text(json.dumps(reports, indent=1))
    print("wrote", out / "report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
