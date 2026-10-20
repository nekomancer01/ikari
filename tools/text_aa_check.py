"""Check that text on Windows is drawn without ClearType's coloured fringes, at a real 150% scaling.

tools/ui_matrix.py fakes scaling through the offscreen platform's DPI, which never shows how Windows
actually draws glyphs. This shows a small part of the sidebar (a form label and a date field, styled by
the real theme) on the real "windows" platform with QT_SCALE_FACTOR=1.5, so the device pixel ratio is
fractional as on a 150% screen, grabs it from the screen and counts coloured pixels in the text.

    python tools/text_aa_check.py            # every variant, a table; exit 1 if the app's own text has fringes
    python tools/text_aa_check.py --out dir  # also save the grabs, enlarged

Windows only: other platforms print a note and exit 0.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name: (extra environment, keep the app's text settings)
VARIANTS = {
    "app": ({}, True),
    "cleartype": ({"QT_QPA_PLATFORM": "windows"}, False),  # before: DirectWrite, ClearType
    "directwrite": ({"QT_QPA_PLATFORM": "windows"}, True),  # DirectWrite with the app's grayscale request
    "freetype": ({"QT_QPA_PLATFORM": "windows:fontengine=freetype"}, True),
    "gdi": ({"QT_QPA_PLATFORM": "windows:fontengine=gdi"}, True),
    "freetype-nohint": ({"QT_QPA_PLATFORM": "windows:fontengine=freetype", "TEXT_AA_NOHINT": "1"}, True),
    "offscreen": ({"QT_QPA_PLATFORM": "offscreen"}, True),  # what tools/ui_matrix.py measures layouts with
}
SAMPLE = "Saved data 1,234 posts, 56,789 comments · 12.3 MB"


def fringe(img, box) -> tuple[int, int]:
    """(coloured pixels, text pixels) inside box. Text is light on a dark field, so a text pixel is one
    noticeably brighter than the field; it's coloured when its channels spread much more than the field's."""
    from PySide6.QtGui import QColor
    x0, y0, x1, y1 = box
    field = QColor(img.pixel(x0 + 1, y0 + 1))
    base_spread = max(field.red(), field.green(), field.blue()) - min(field.red(), field.green(), field.blue())
    base_light = field.lightness()
    coloured = text = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            c = QColor(img.pixel(x, y))
            if c.lightness() - base_light < 25:
                continue
            text += 1
            spread = max(c.red(), c.green(), c.blue()) - min(c.red(), c.green(), c.blue())
            if spread - base_spread > 40:
                coloured += 1
    return coloured, text


def child(name: str, keep: bool, out: str | None) -> dict:
    sys.path.insert(0, str(ROOT / "src"))
    from PySide6.QtCore import QDate, Qt, QTimer
    from PySide6.QtGui import QFont, QGuiApplication
    from PySide6.QtWidgets import QApplication, QDateEdit, QFormLayout, QFrame, QLabel

    from subsentiment.gui import theme
    if not keep:
        theme.smooth_text = lambda f: f
    app = QApplication(theme.qt_args(["text_aa_check"]))
    theme.load_fonts(app)
    if os.environ.get("TEXT_AA_NOHINT"):
        f = app.font()
        f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        app.setFont(f)
    theme.apply(app, "dark")

    panel = QFrame()
    panel.setObjectName("sidebar")
    panel.setStyleSheet("QFrame#sidebar { background: #2b3446; }")
    form = QFormLayout(panel)
    form.setContentsMargins(16, 16, 16, 16)
    date = QDateEdit(QDate(2026, 10, 20))
    date.setDisplayFormat("d MMM yyyy")
    date.setCalendarPopup(True)
    date.setMinimumWidth(200)
    date.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # no selection or cursor over the text
    date.lineEdit().setFocusPolicy(Qt.FocusPolicy.NoFocus)
    form.addRow(QLabel("To"), date)
    panel.move(40, 40)
    panel.show()
    panel.raise_()
    panel.activateWindow()

    result: dict = {}

    def grab():
        dpr = panel.devicePixelRatioF()
        shots = {"screen": QGuiApplication.primaryScreen().grabWindow(panel.winId()).toImage(),
                 "widget": panel.grab().toImage()}
        edit = date.lineEdit()
        tl = edit.mapTo(panel, edit.rect().topLeft())
        box = [round(tl.x() * dpr), round(tl.y() * dpr),
               round((tl.x() + edit.width()) * dpr), round((tl.y() + edit.height()) * dpr)]
        f = app.font()
        result.update(variant=name, qt=__import__("PySide6").__version__, platform=app.platformName(),
                      dpr=dpr, strategy=int(f.styleStrategy().value), advance=date.fontMetrics().horizontalAdvance(SAMPLE),
                      engine=os.environ.get("QT_QPA_PLATFORM", ""), hinting=str(f.hintingPreference()),
                      no_subpixel=bool(f.styleStrategy() & QFont.StyleStrategy.NoSubpixelAntialias))
        for kind, img in shots.items():
            img = img.convertToFormat(img.Format.Format_RGB32)
            b = [min(v, lim - 1) for v, lim in zip(box, (img.width(), img.height()) * 2)]
            result[kind] = dict(zip(("coloured", "text"), fringe(img, b)))
            crop = img.copy(b[0], b[1], b[2] - b[0], b[3] - b[1])
            if out:
                crop.scaled(crop.width() * 6, crop.height() * 6).save(str(Path(out) / f"{name}-{kind}.png"))
        app.quit()

    QTimer.singleShot(1500, grab)
    app.exec()
    return result


def window_shots(name: str, keep: bool, out: str) -> None:
    """The whole main window with sample data at the sizes a 150% and a 125% screen give it, for a
    before/after of the text on the real platform (tools/ui_matrix.py's shots use the offscreen one)."""
    import tempfile
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT / "tools"))
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication

    import screenshot
    from subsentiment.gui import main_window, theme
    if not keep:
        theme.smooth_text = lambda f: f
    db = str(Path(tempfile.mkdtemp(prefix="ikari-text-")) / "sample.db")
    screenshot.seed(db)
    app = QApplication(theme.qt_args(["text_aa_check"]))
    app.setOrganizationName("ikari-screenshots")
    app.setApplicationName(f"ikari-text-{name}")
    settings = QSettings()
    settings.clear()
    settings.setValue("updates/auto", False)
    settings.setValue("ui/last_dataset", screenshot.SUBREDDIT)
    theme.load_fonts(app)
    theme.apply(app, "dark")
    window = main_window.MainWindow(db)
    window.subreddit.setText(screenshot.SUBREDDIT)
    window.show()
    scale = os.environ.get("QT_SCALE_FACTOR", "1")
    sizes = {"1.5": [("maximized", 1280, 721), ("restored", 1203, 691)], "1.25": [("maximized", 1093, 614)]}[scale]
    for state, w, h in sizes:
        window.resize(w, h)
        for _ in range(30):
            app.processEvents()
        window.grab().save(str(Path(out) / f"{name}-{round(float(scale) * 100)}-{state}.png"))
    window.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant")
    ap.add_argument("--out")
    ap.add_argument("--window", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    if args.out:
        Path(args.out).mkdir(parents=True, exist_ok=True)
    if args.variant:
        env, keep = VARIANTS[args.variant]
        if args.window:
            window_shots(args.variant, keep, args.out)
        else:
            print(json.dumps(child(args.variant, keep, args.out)))
        return 0
    if sys.platform != "win32":
        print("text_aa_check: Windows only, nothing to check here")
        return 0
    rows = []
    for name, (env, _) in VARIANTS.items():
        e = {**os.environ, "QT_SCALE_FACTOR": "1.5", **env}
        e.pop("QT_QPA_PLATFORMTHEME", None)
        cmd = [sys.executable, __file__, "--variant", name] + (["--out", args.out] if args.out else [])
        p = subprocess.run(cmd, env=e, capture_output=True, text=True, timeout=120)
        line = (p.stdout.strip().splitlines() or ["{}"])[-1]
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"variant": name, "error": (p.stderr or p.stdout)[-800:]})
    if args.out:  # before (DirectWrite, ClearType) and after, whole window
        for name in ("cleartype", "app"):
            for scale in ("1.5", "1.25"):
                e = {**os.environ, "QT_SCALE_FACTOR": scale, **VARIANTS[name][0]}
                subprocess.run([sys.executable, __file__, "--variant", name, "--window", "--out", args.out],
                               env=e, timeout=180, check=False)
    print("| variant | platform | dpr | sample width | screen coloured/text | widget coloured/text |")
    print("|---|---|---|---|---|---|")
    for r in rows:
        if "error" in r:
            print(f"| {r['variant']} | failed | | | {r['error']!r} | |")
            continue
        s, w = r["screen"], r["widget"]
        print(f"| {r['variant']} | {r['platform']} | {r['dpr']} | {r['advance']} | {s['coloured']}/{s['text']} | "
              f"{w['coloured']}/{w['text']} |")
    app = next((r for r in rows if r.get("variant") == "app" and "screen" in r), None)
    if app is None:
        print("the app variant didn't render")
        return 1
    if app["screen"]["coloured"] or app["widget"]["coloured"]:
        print("FAIL: the app's text has coloured (ClearType) fringes")
        return 1
    # The layout checks measure text offscreen; the app's real text must not be much wider, or what passes
    # there could be cut off here.
    off = next((r for r in rows if r.get("variant") == "offscreen" and "advance" in r), None)
    if off and app["advance"] > off["advance"] * 1.02:
        print(f"FAIL: the app's text is {app['advance'] / off['advance'] - 1:.1%} wider than the layout checks measure")
        return 1
    print("OK: no coloured fringes in the app's text")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
