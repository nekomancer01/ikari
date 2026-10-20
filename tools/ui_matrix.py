"""Render the main window on common screens and scalings, and flag layout problems.

    python tools/ui_matrix.py --out shots/            # every screen, restored + maximized + a resize sweep
    python tools/ui_matrix.py --only win-1920x1200    # one screen, every scaling listed for it
    python tools/ui_matrix.py --only 150 --theme light
    python tools/ui_matrix.py --no-shots --strict     # checks only; exit status 1 on any problem (tests use this)

Each screen runs in its own Qt process on the offscreen platform, configured with that screen's size and
device pixel ratio, and with the font DPI the real OS uses (Windows and Linux 96, macOS 72, which makes the same
point size smaller on a Mac). The taskbar, Dock, menu bar and title bar are taken off the free area by
hand, since the offscreen platform has none. Restored is the size the app opens at; maximized is the free
area; the sweep drags the window from maximized down to its minimum size in a few steps.

Checks on every size, for the main window and the Download coverage window: the window can't shrink to fit
the screen; a label's text is cut off; a field or button is squashed below what its text needs; a widget
sticks out of its parent or overlaps a neighbour; the sidebar fields, form labels, header, tiles and cards
are out of line; the sidebar scrolls on a window tall enough not to need it. It also notes which scroll bars
show (some are on purpose). Writes one PNG per size, report.md and report.json. Uses the same made-up sample
data as tools/screenshot.py; your own data and settings are not touched.
"""
from __future__ import annotations

import argparse
import concurrent.futures
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
LINUX_PANEL, LINUX_TITLE = 32, 37  # GNOME's top bar and a client-side title bar
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
    ("linux-1366x768", "linux", 1366, 768, [100]),
    ("linux-1920x1080", "linux", 1920, 1080, [100, 125]),
    ("linux-1920x1200", "linux", 1920, 1200, [100, 150]),
    ("linux-2560x1440", "linux", 2560, 1440, [100, 150]),
    ("linux-3840x2160", "linux", 3840, 2160, [200]),
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
    if os_name == "linux":
        return int(w), int(h - LINUX_PANEL - LINUX_TITLE)
    menu = MAC_MENU_NOTCH if screen in NOTCHED else MAC_MENU
    return int(w), int(h - menu - MAC_TITLE - MAC_DOCK)


# -- checks ------------------------------------------------------------------------------------

TOL = 1  # px of slack for rounding


def _name(w) -> str:
    bits = [type(w).__name__]
    text = getattr(w, "text", None)
    if callable(text) and isinstance(text(), str) and text():
        bits.append(repr(text()[:40]))
    elif w.objectName():
        bits.append(w.objectName())
    return " ".join(bits)


def _laid_out(widget) -> list:
    """The visible widgets a widget's own layout places (through nested layouts, not into child widgets)."""
    found, stack = [], [widget.layout()] if widget.layout() else []
    while stack:
        lay = stack.pop()
        for i in range(lay.count()):
            item = lay.itemAt(i)
            if item.widget() is not None:
                if item.widget().isVisible():
                    found.append(item.widget())
            elif item.layout() is not None:
                stack.append(item.layout())
    return found


def check(window) -> list[str]:
    """Generic layout problems anywhere in a top-level window right now."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QAbstractButton, QAbstractScrollArea, QAbstractSpinBox, QComboBox, QLabel, QLineEdit, QScrollArea,
        QTabBar, QWidget,
    )

    def scrolled(w: QWidget) -> bool:
        """Inside a scroll area's contents, where being out of view is fine."""
        p = w.parentWidget()
        while p is not None and p is not window:
            if isinstance(p, QScrollArea) or (isinstance(p, QAbstractScrollArea) and w is not p):
                return True
            p = p.parentWidget()
        return False

    problems = []
    for w in [window, *window.findChildren(QWidget)]:
        if not w.isVisible() or w.window() is not window or w.width() <= 0 or w.visibleRegion().isEmpty():
            continue  # hidden, or scrolled out of view (table rows below the fold keep stale sizes)
        if isinstance(w, QLabel) and QLabel.text(w) and not w.pixmap() and "<" not in QLabel.text(w):  # shown text
            m = w.fontMetrics()
            room = w.contentsRect()
            if w.wordWrap():
                need = w.heightForWidth(w.width())
                if need > w.height() + TOL:
                    problems.append(f"cut off (wrapped text needs {need}px, has {w.height()}): {_name(w)}")
            elif m.horizontalAdvance(QLabel.text(w)) > room.width() + TOL:
                problems.append(f"cut off ({m.horizontalAdvance(QLabel.text(w))}px text in {room.width()}): {_name(w)}")
        if isinstance(w, (QAbstractButton, QComboBox, QLineEdit, QAbstractSpinBox, QTabBar)):
            hint = w.minimumSizeHint().boundedTo(w.maximumSize())  # a style cap is on purpose
            if w.width() + TOL < hint.width() or w.height() + TOL < hint.height():
                problems.append(f"squashed ({w.width()}x{w.height()}, needs {hint.width()}x{hint.height()}): "
                                f"{_name(w)}")
        parent = w.parentWidget()
        if w is not window and parent is not None and parent.isVisible() and not scrolled(w) and not w.isWindow():
            g, r = w.geometry(), parent.rect()
            if g.right() > r.right() + TOL or g.bottom() > r.bottom() + TOL or g.left() < -TOL or g.top() < -TOL:
                problems.append(f"sticks out of {type(parent).__name__} ({g.x()},{g.y()} {g.width()}x{g.height()} "
                                f"in {r.width()}x{r.height()}): {_name(w)}")
        siblings = _laid_out(w)
        for i, a in enumerate(siblings):
            for b in siblings[i + 1:]:
                both = a.geometry().intersected(b.geometry())
                if both.width() > TOL and both.height() > TOL:
                    problems.append(f"overlaps ({both.width()}x{both.height()} px): {_name(a)} and {_name(b)}")
    for area in window.findChildren(QScrollArea):
        inner = area.widget()
        sideways_off = area.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        if area.isVisible() and inner is not None and sideways_off and inner.width() > area.viewport().width() + TOL:
            problems.append(f"cut off sideways ({inner.width()}px wide in {area.viewport().width()}): "
                            f"scroll area of {type(inner.layout()).__name__ if inner.layout() else 'widget'}")
    return problems


def _rect(w, top):
    """w's rectangle in top's coordinates."""
    from PySide6.QtCore import QRect
    return QRect(w.mapTo(top, w.rect().topLeft()), w.size())


def _same(problems: list, what: str, values: dict) -> None:
    """Note a problem when the named values differ by more than the slack."""
    if values and max(values.values()) - min(values.values()) > TOL:
        problems.append(f"out of line, {what}: " + ", ".join(f"{k} {v}" for k, v in values.items()))


def alignment(window) -> list[str]:
    """Main-window alignment rules: things that should share an edge or a centre do."""
    from PySide6.QtWidgets import QFormLayout, QLabel

    problems = []
    side = window.findChild(QFormLayout).parentWidget()  # the sidebar's scrolling body
    f = window.fetch_form
    fields = {"subreddit": f.subreddit, "search": f.query, "posted": f.range, "from": f.date_from, "to": f.date_to,
              "comments": f.comments_per_post.parentWidget()}
    _same(problems, "sidebar field left edges", {k: _rect(w, side).left() for k, w in fields.items()})
    _same(problems, "sidebar field right edges", {k: _rect(w, side).right() for k, w in fields.items()})
    _same(problems, "sidebar field heights", {k: w.height() for k, w in fields.items() if k != "comments"})
    _same(problems, "sidebar checkbox left edges",
          {"fetch": _rect(f.include_comments, side).left(), "scores": _rect(f.refresh_scores, side).left(),
           "fields": _rect(f.subreddit, side).left()})
    form = window.findChild(QFormLayout)
    labels = {}
    for row in range(form.rowCount()):
        lab = form.itemAt(row, QFormLayout.ItemRole.LabelRole)
        field = form.itemAt(row, QFormLayout.ItemRole.FieldRole)
        if lab and field and lab.widget() and lab.widget().isVisible():
            text = lab.widget().text()
            labels[text] = lab.widget().geometry().left()
            dy = abs(lab.widget().geometry().center().y() - field.geometry().center().y())
            if dy > TOL:
                problems.append(f"out of line, form label {text!r} is {dy}px off its field's centre")
    _same(problems, "form label left edges", labels)
    # Equal margins left and right of the sidebar's contents (the scroll bar, when it shows, sits in the right one).
    left = _rect(form.itemAt(0, QFormLayout.ItemRole.LabelRole).widget(), side).left()
    right = side.width() - 1 - max(_rect(w, side).right() for w in [*fields.values(), window.fetch_btn, window.stop_btn])
    if sidebar_scrolls(window):
        right += side.parentWidget().parentWidget().verticalScrollBar().width()
    _same(problems, "sidebar margins", {"left": left, "right": right})
    _same(problems, "Fetch row button tops", {"fetch": _rect(window.fetch_btn, side).top(),
                                               "stop": _rect(window.stop_btn, side).top()})
    _same(problems, "Fetch row button heights", {"fetch": window.fetch_btn.height(), "stop": window.stop_btn.height()})
    title = next(lab for lab in side.findChildren(QLabel) if lab.property("role") == "title")
    _same(problems, "sidebar left edges", {"form labels": _rect(form.itemAt(0, QFormLayout.ItemRole.LabelRole)
                                                                 .widget(), side).left(),
                                            "title": _rect(title, side).left(),
                                            "fetch button": _rect(window.fetch_btn, side).left(),
                                            "saved list": _rect(window.datasets, side).left(),
                                            "storage line": _rect(window.storage_info, side).left()})
    _same(problems, "sidebar right edges", {"fields": max(_rect(w, side).right() for w in fields.values()),
                                             "stop": _rect(window.stop_btn, side).right(),
                                             "saved list": _rect(window.datasets, side).right(),
                                             "delete all": _rect(window.delete_all_btn, side).right()})

    # The content column: heading, subtitle and the page below share a left edge; tabs end at its right.
    page = window.pages.currentWidget()
    if window.tabbar.currentIndex() == 0:
        tiles = list(window.overview.tiles.values())
        body = [_rect(t, window) for t in tiles]
        _same(problems, "content left edges", {"heading": _rect(window.heading, window).left(),
                                                "subtitle": _rect(window.subheading, window).left(),
                                                "first tile": body[0].left()})
        _same(problems, "tile widths", {t.findChild(QLabel).text(): t.width() for t in tiles})
        _same(problems, "tile tops", {t.findChild(QLabel).text(): _rect(t, window).top() for t in tiles})
        o = window.overview
        cards = [_rect(c.parentWidget(), window) for c in (o.distribution, o.timeline, o.most_positive, o.most_negative)]
        _same(problems, "overview left edges", {"tiles": body[0].left(), "charts": cards[0].left(),
                                                 "lists": cards[2].left()})
        _same(problems, "overview right edges", {"tiles": body[-1].right(), "charts": cards[1].right(),
                                                  "lists": cards[3].right()})
        _same(problems, "chart card tops and heights", {"distribution top": cards[0].top(),
                                                        "timeline top": cards[1].top()})
        _same(problems, "list card heights", {"positive": cards[2].height(), "negative": cards[3].height()})
        content_right = body[-1].right()
    else:
        content_right = _rect(page, window).right()
        _same(problems, "content left edges", {"heading": _rect(window.heading, window).left(),
                                                "subtitle": _rect(window.subheading, window).left(),
                                                "table card": _rect(page, window).left()})
    last = window.tabbar.count() - 1
    tab_right = window.tabbar.mapTo(window, window.tabbar.tabRect(last).topRight()).x()
    slack = 4 + TOL  # tabs carry a 4 px right margin in the stylesheet
    bar = page.verticalScrollBar() if hasattr(page, "verticalScrollBar") else None
    if bar is not None and bar.isVisible():
        slack += bar.width()  # a scrolling overview gives its scroll bar the last few pixels
    if abs(tab_right - content_right) > slack:
        problems.append(f"out of line, tabs end at {tab_right} but the content ends at {content_right}")
    # Header controls share a vertical centre with each other.
    if window.scope.isVisible():
        selected = window.tabbar.tabRect(window.tabbar.currentIndex())
        tab_mid = window.tabbar.mapTo(window, selected.center()).y()
        scope_mid = _rect(window.scope, window).center().y()
        if abs(tab_mid - scope_mid) > 2:
            problems.append(f"out of line, scope picker centre {scope_mid} vs tabs {tab_mid}")
    # Short labels that should never wrap or end in "…".
    fm = window.storage_info.fontMetrics()
    if fm.horizontalAdvance(window.storage_info.text()) > window.storage_info.width():
        problems.append(f"wraps, storage line: {window.storage_info.text()!r}")
    if window.width() >= 1200 and window.subheading.text() != window.subheading.full_text():
        problems.append(f"ends in '…' on a {window.width()} px window: {window.subheading.full_text()!r}")
    return problems


def coverage_alignment(dialog) -> list[str]:
    problems = []
    panels = [dialog.day_table.parentWidget(), dialog.post_table.parentWidget()]
    _same(problems, "coverage panel tops", {"days": _rect(panels[0], dialog).top(), "posts": _rect(panels[1], dialog).top()})
    _same(problems, "coverage table tops", {"days": _rect(dialog.day_table, dialog).top(),
                                             "posts": _rect(dialog.post_table, dialog).top()})
    _same(problems, "coverage panel bottoms",
          {"days": _rect(panels[0], dialog).bottom(), "posts": _rect(panels[1], dialog).bottom()})
    return problems


def table_room(window) -> list[str]:
    """The Posts or Comments table leaves its main text column (title, comment) real room."""
    problems = []
    for name, page in (("posts", window.posts_tab), ("comments", window.comments_tab)):
        if not page.isVisible() or not page.model.rowCount():
            continue
        room = page.view.viewport().width()
        text = page.view.horizontalHeader().sectionSize(page.stretch_col)
        if text < room * 0.3:
            problems.append(f"squashed, the {name} table's text column is {text}px of {room}")
    return problems


def field_edges(window) -> dict:
    from PySide6.QtWidgets import QFormLayout
    side = window.findChild(QFormLayout).parentWidget()
    f = window.fetch_form
    return {name: (_rect(w, side).left(), _rect(w, side).right(), _rect(w, window).top())
            for name, w in (("subreddit", f.subreddit), ("to", f.date_to), ("posted", f.range))}


# A status line as long as the app writes, during a fetch and after it.
STATUS = ("Added 1,234 new posts and 5,678 comments from r/AskHistorians. Updated scores on 321 saved posts. "
          "12 top posts were already saved. Comments couldn't be fetched for 3 posts; try again later.")


def sidebar_scrolls(window) -> bool:
    from PySide6.QtWidgets import QFormLayout
    body = window.findChild(QFormLayout).parentWidget()
    area = body.parentWidget().parentWidget()  # viewport, then the scroll area
    bar = area.verticalScrollBar()
    return bar.isVisible() and bar.maximum() > 0


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


# -- child: one screen, one scaling ------------------------------------------------------------

SIDEBAR_FITS = 715  # logical px of window height from which the sidebar must not scroll (1920x1200 150% is 721)


def child(spec: dict) -> int:
    from PySide6.QtCore import QRect, QSettings, QSize
    from PySide6.QtWidgets import QApplication

    from subsentiment.gui import coverage, main_window, motion, native, theme, widgets

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import screenshot  # noqa: E402  (seeds the sample data)

    out = Path(spec["out"])
    native.setup_app_identity()
    app = QApplication(sys.argv[:1])
    app.setOrganizationName("ikari-screenshots")
    app.setApplicationName(f"ikari-matrix-{spec['tag']}")  # parallel runs don't share a settings file
    app.setApplicationDisplayName("ikari")
    settings = QSettings()
    settings.clear()
    settings.setValue("updates/auto", False)
    settings.setValue("ui/theme", spec["theme"])
    settings.setValue("ui/tab", 0)
    settings.setValue("ui/last_dataset", screenshot.SUBREDDIT)
    if spec.get("subpixel"):  # the old look, for a before/after of the text rendering
        theme.smooth_text = lambda f: f
    theme.load_fonts(app)
    theme.apply(app, spec["theme"])  # before the window, as the app does, so first sizes are the styled ones

    window = main_window.MainWindow(spec["db"])
    motion.set_enabled(False)  # sizes and stills of the finished state, not mid-fade
    window.fetch_form.subreddit.setText(screenshot.SUBREDDIT)
    window.tabbar.setFocus()

    class Free:  # the screen's free area, for the app's own opening-size rule
        def __init__(self, w, h):
            self.rect = QRect(0, 0, w, h + {"win": WIN_CAPTION, "linux": LINUX_TITLE}.get(spec["os"], MAC_TITLE))

        def availableGeometry(self):
            return self.rect

    fw, fh = spec["free"]

    def settle():
        for _ in range(6):
            app.processEvents()
            time.sleep(0.02)

    def sizes_for(top, opening: QSize) -> list:
        top.resize(opening)
        top.show()
        settle()
        minimum = top.minimumSizeHint().expandedTo(top.minimumSize())
        sizes = [("restored", opening.width(), opening.height()), ("maximized", fw, fh)]
        if spec["sweep"]:
            for i in range(1, 5):  # maximized down to the minimum, both ways, then each way alone
                f = i / 4
                sizes.append((f"sweep-{i}", round(fw - (fw - minimum.width()) * f),
                              round(fh - (fh - minimum.height()) * f)))
            sizes.append(("narrow", minimum.width(), fh))
            sizes.append(("short", fw, minimum.height()))
        return minimum, sizes

    def measure(top, w, h, state, extra) -> dict:
        top.resize(w, h)
        settle()
        assert abs(top.devicePixelRatio() - spec["dpr"]) < 0.01, top.devicePixelRatio()
        got = top.size()
        problems = []
        if got.width() > fw or got.height() > fh:
            problems.append(f"window can't shrink to fit: {got.width()}x{got.height()} on a {fw}x{fh} free area")
        problems += check(top) + extra()
        shot = f"{spec['tag']}-{state.replace(' ', '-')}.png"
        if spec["shots"]:
            top.grab().save(str(out / shot))
        return {"state": state, "size": [got.width(), got.height()], "asked": [w, h], "problems": problems,
                "scrollbars": scrollbars(top), "shot": shot}

    def main_rules():
        found = alignment(window) + table_room(window)
        if window.height() >= SIDEBAR_FITS and sidebar_scrolls(window):
            found.append(f"sidebar scrolls on a {window.height()} px tall window")
        return found

    results = []
    minimum, sizes = sizes_for(window, widgets.fit_to_screen(QSize(1320, 860), Free(fw, fh)))
    for state, w, h in sizes:
        for tab in ([0, 1, 2] if state in ("restored", "maximized") else [0]):
            window.tabbar.setCurrentIndex(tab)
            results.append(measure(window, w, h, state + ["", " posts", " comments"][tab], main_rules))
    window.tabbar.setCurrentIndex(0)

    # A status line under Fetch, with and without the progress bar: the sidebar's fields must not move
    # and it must not start scrolling.
    for state, w, h in sizes[:2]:
        window.resize(w, h)
        settle()
        idle = field_edges(window)
        for phase, busy in (("fetching", True), ("finished", False)):
            window.progress.setVisible(busy)
            window.progress_msg.setText(STATUS)

            def status_rules():
                found = main_rules()
                moved = {k: (idle[k], v) for k, v in field_edges(window).items() if v != idle[k]}
                if moved:
                    found.append(f"out of line, sidebar fields move when a status line shows: {moved}")
                return found
            results.append(measure(window, w, h, f"{state} {phase}", status_rules))
        window.progress.setVisible(False)
        window.progress_msg.setText("")

    # The Download coverage window, with made-up archive counts instead of asking the network.
    def fake_count(self, after, before):
        self._on_counts(self.subreddit, {d: 12 for d in range(int(after), int(before), 86400)})
    coverage.CoverageWindow._count = fake_count
    window.fetch_form.range.setCurrentIndex(window.fetch_form.range.findData("month"))
    window._show_coverage()
    dialog = window.coverage
    if dialog is None:
        results.append({"state": "coverage", "size": [0, 0], "asked": [0, 0], "scrollbars": [], "shot": "",
                        "problems": ["the Download coverage window didn't open"]})
    else:
        dialog.day_table.selectRow(0)
        cov_min, cov_sizes = sizes_for(dialog, widgets.fit_to_screen(QSize(1120, 720), Free(fw, fh)))
        for state, w, h in cov_sizes:
            results.append(measure(dialog, w, h, f"coverage {state}", lambda: coverage_alignment(dialog)))
        dialog.close()
    print(json.dumps({"tag": spec["tag"], "minimum": [minimum.width(), minimum.height()], "results": results}))
    window.close()
    return 0


# -- parent: the whole matrix ------------------------------------------------------------------


def screens(only: list[str] | None = None) -> list[tuple]:
    """(tag, os, physical width, physical height, scaling %) for every screen and scaling, filtered by --only."""
    rows = [(f"{screen}-{scale}", os_name, pw, ph, scale) for screen, os_name, pw, ph, scales in SCREENS
            for scale in scales]
    return [r for r in rows if not only or any(o in r[0] for o in only)]


def run_one(row, db: Path, tmp: Path, out: Path, args) -> dict | str:
    tag, os_name, pw, ph, scale = row
    dpr = scale / 100
    lw, lh = pw / dpr, ph / dpr
    dpi = 72 if os_name == "mac" else 96
    config = tmp / f"{tag}.json"
    # Qt takes the scaling from logical DPI over base DPI, as it does from Windows' display setting.
    config.write_text(json.dumps({"screens": [{
        "name": tag, "x": 0, "y": 0, "width": pw, "height": ph,
        "logicalDpi": dpi * dpr, "logicalBaseDpi": dpi, "dpr": 1}]}))
    screen = tag.rsplit("-", 1)[0]
    spec = {"tag": tag, "os": os_name, "dpr": dpr, "free": free_area(screen, os_name, lw, lh), "db": str(db),
            "out": str(out), "theme": args.theme, "sweep": not args.no_sweep, "subpixel": args.subpixel,
            "shots": not args.no_shots}
    # A bare file name, run from its folder: a Windows drive colon would end the platform name early.
    env = dict(os.environ, QT_QPA_PLATFORM=f"offscreen:configfile={config.name}")
    env.pop("QT_SCALE_FACTOR", None)
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", json.dumps(spec)],
                          env=env, cwd=tmp, capture_output=True, text=True, check=False)
    lines = proc.stdout.strip().splitlines()
    if proc.returncode or not lines:
        return f"{tag}: failed to render\n{proc.stderr[-3000:]}"
    report = json.loads(lines[-1])
    report["logical"] = [round(lw), round(lh)]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default="ui-matrix", help="folder for the PNGs and report.md")
    parser.add_argument("--only", action="append", default=[], help="keep screens/scalings whose tag has this text")
    parser.add_argument("--theme", default="dark", choices=["dark", "light"])
    parser.add_argument("--no-sweep", action="store_true", help="skip the resize sweep")
    parser.add_argument("--no-shots", action="store_true", help="check only, don't save PNGs")
    parser.add_argument("--jobs", type=int, default=0, help="screens rendered at once (default: one per CPU)")
    parser.add_argument("--strict", action="store_true", help="exit with status 1 if any check finds a problem")
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

    rows = screens(args.only)
    jobs = args.jobs or os.cpu_count() or 2
    reports, failures = [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        for row, result in zip(rows, pool.map(lambda r: run_one(r, db, tmp, out, args), rows)):
            if isinstance(result, str):
                failures.append(result)
                print(result, file=sys.stderr)
                continue
            reports.append(result)
            bad = sum(bool(r["problems"]) for r in result["results"])
            print(f"{row[0]:24} logical {result['logical'][0]}x{result['logical'][1]}  min {result['minimum']}  "
                  f"{'OK' if not bad else f'{bad} sizes with problems'}", flush=True)

    lines = ["# ikari layout matrix", "",
             f"Theme {args.theme}. Sizes are the window's contents in logical pixels.", "",
             "| Screen | Logical | Minimum | Size | Window | Problems | Scroll bars |", "|---|---|---|---|---|---|---|"]
    for rep in reports:
        for r in rep["results"]:
            lines.append(f"| {rep['tag']} | {rep['logical'][0]}x{rep['logical'][1]} | "
                         f"{rep['minimum'][0]}x{rep['minimum'][1]} | {r['state']} | {r['size'][0]}x{r['size'][1]} | "
                         f"{'<br>'.join(r['problems']) or 'none'} | {', '.join(r['scrollbars']) or 'none'} |")
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps(reports, indent=1), encoding="utf-8")
    print("wrote", out / "report.md")
    problems = sum(len(r["problems"]) for rep in reports for r in rep["results"])
    if args.strict and (problems or failures):
        print(f"{problems} layout problems, {len(failures)} screens failed to render", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
