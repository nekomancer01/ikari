"""Glass theme: a soft gradient backdrop with translucent, rounded panels on top.

Qt widgets can't blur what's behind them, so the "frosted" look comes from
translucent fills over a smooth, low-contrast backdrop, plus a brighter top
edge on each panel to catch the light. Following common glassmorphism
guidance, glass is kept to chrome and summary cards; dense data (tables,
lists) sits on a near-solid surface, and glass is never stacked on glass.
A faint tiled grain on the glass gives it a frosted, physical texture.
"Reduce transparency" swaps every fill for a near-opaque one and drops the grain.

The backdrop can also carry the mood of the subreddit on screen: MoodTint pulls the glows and a
soft wash toward green when it skews positive, white when it's neutral and red when it skews
negative, easing between subreddits. It's a wash, not a fill, so panel and text contrast hold.

Type: Manrope for the interface and Sora for headings and big numbers, both
SIL Open Font License, bundled in assets/. Tabular figures are switched on so
numbers line up in columns.
"""
from __future__ import annotations

import random
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import (
    QColor, QFont, QFontDatabase, QGuiApplication, QImage, QLinearGradient, QPainter, QPalette,
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication, QWidget

from subsentiment.gui import native

ASSETS = Path(__file__).parent / "assets"

THEMES = {
    "light": {
        "bg0": "#e8edfa", "bg1": "#f5f1fb",
        "blobs": [("#6f97ff", 120), ("#b190ff", 95), ("#5fd6c4", 80)],
        "glass": (255, 255, 255, 150), "glass_strong": (255, 255, 255, 205), "glass_soft": (255, 255, 255, 95),
        "solid": (250, 251, 255, 236),
        "edge_hi": (255, 255, 255, 235), "edge": (40, 52, 96, 34),
        "text": "#121624", "muted": "#5a6279", "faint": "#8a91a6",
        "accent": "#3d6bff", "accent2": "#7a5cff", "on_accent": "#ffffff",
        "button0": "#3d6bff", "button1": "#7a5cff",
        "selection": (61, 107, 255, 40), "menu": "#f7f8fd", "shadow": (40, 52, 110, 40),
        "grain": ((20, 26, 48, 11), (255, 255, 255, 20)),  # (dark speck, light speck), peak alpha
    },
    "dark": {
        "bg0": "#0c0f1d", "bg1": "#151a2f",
        "blobs": [("#2f55ff", 95), ("#8a3dff", 80), ("#00a99a", 60)],
        "glass": (255, 255, 255, 18), "glass_strong": (255, 255, 255, 30), "glass_soft": (255, 255, 255, 10),
        "solid": (20, 25, 42, 228),
        "edge_hi": (255, 255, 255, 46), "edge": (255, 255, 255, 20),
        "text": "#eef1f8", "muted": "#a7afc5", "faint": "#737b93",
        "accent": "#6d8dff", "accent2": "#9a7bff", "on_accent": "#ffffff",
        "button0": "#3f5fe8", "button1": "#6e45e0",  # deeper than the accent so white text keeps contrast
        "selection": (109, 141, 255, 60), "menu": "#1b2034", "shadow": (0, 0, 0, 120),
        "grain": ((0, 0, 0, 17), (255, 255, 255, 8)),
    },
}

UI_FAMILIES = ["Manrope", "Segoe UI Variable Text", "SF Pro Text", "Segoe UI", "Noto Sans"]
DISPLAY_FAMILY = "Sora"

# Near-opaque replacements used when transparency is reduced.
OPAQUE = {
    "light": {"glass": (246, 248, 253, 255), "glass_strong": (255, 255, 255, 255), "glass_soft": (236, 239, 248, 255),
              "solid": (255, 255, 255, 255)},
    "dark": {"glass": (24, 29, 46, 255), "glass_strong": (33, 39, 58, 255), "glass_soft": (28, 33, 51, 255),
             "solid": (22, 27, 43, 255)},
}

# Sentiment wash over the backdrop: (rgb, peak alpha) per mood, and how far the glows lean toward it.
MOOD = {
    "light": {"positive": ((46, 196, 108), 70), "neutral": ((255, 255, 255), 120), "negative": ((236, 72, 72), 66),
              "blob_mix": 0.75},
    "dark": {"positive": ((34, 170, 92), 60), "neutral": ((214, 222, 240), 22), "negative": ((214, 52, 60), 64),
             "blob_mix": 0.7},
}

_mode = "light"
_reduce = False
_icon_dir: Path | None = None


def mode() -> str:
    return _mode


def tokens() -> dict:
    return dict(THEMES[_mode], **OPAQUE[_mode]) if _reduce else THEMES[_mode]


def smooth_text(f: QFont) -> QFont:
    """Grayscale antialiasing on Windows instead of ClearType's colored edges.

    At fractional scaling (125%, 150%) ClearType's red and blue fringes land on half pixels and over the
    glass and backdrop colors, which made text look frayed, worst in a maximized window. Grayscale
    edges stay clean at those densities, as in most modern Windows apps."""
    if sys.platform == "win32":
        f.setStyleStrategy(QFont.StyleStrategy(QFont.StyleStrategy.PreferAntialias
                                               | QFont.StyleStrategy.NoSubpixelAntialias))
    return f


def display_font(point_size: float, weight: int = 600) -> QFont:
    f = QFont(DISPLAY_FAMILY)
    f.setPointSizeF(point_size)
    f.setWeight(QFont.Weight(weight))
    return smooth_text(f)


def system_mode() -> str:
    hints = QGuiApplication.styleHints()
    scheme = hints.colorScheme() if hasattr(hints, "colorScheme") else Qt.ColorScheme.Unknown
    if scheme == Qt.ColorScheme.Dark:
        return "dark"
    if scheme == Qt.ColorScheme.Light:
        return "light"
    return native.fallback_color_scheme() or "light"  # some Linux desktops don't report one to Qt


def _rgba(c: tuple) -> str:
    return f"rgba({c[0]}, {c[1]}, {c[2]}, {c[3]})"


def load_fonts(app: QApplication) -> None:
    # Static weights cut from the variable fonts: Qt's variable-font weight
    # matching is unreliable across versions, static files always match.
    for path in sorted(ASSETS.glob("*.ttf")):
        QFontDatabase.addApplicationFont(str(path))
    font = QFont()
    font.setFamilies(UI_FAMILIES)
    font.setPointSizeF(10)
    font.setWeight(QFont.Weight.Medium)  # Manrope's regular is light on translucent fills
    if sys.platform != "win32":  # Windows text is crisper with its default (DirectWrite) hinting
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    font.setFeature(QFont.Tag("tnum"), 1)
    app.setFont(smooth_text(font))


def _icons(t: dict) -> Path:
    """Write the small SVG glyphs the stylesheet needs, colored for this theme."""
    global _icon_dir
    if _icon_dir is None:
        _icon_dir = Path(tempfile.gettempdir()) / "subsentiment-icons"
        _icon_dir.mkdir(exist_ok=True)
    chevron = ('<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 12 12">'
               '<path d="M{d}" fill="none" stroke="{c}" stroke-width="1.6" stroke-linecap="round" '
               'stroke-linejoin="round"/></svg>')
    files = {
        "down": chevron.format(d="2.5 4.5 L6 8 L9.5 4.5", c=t["muted"]),
        "up": chevron.format(d="2.5 7.5 L6 4 L9.5 7.5", c=t["muted"]),
        "check": chevron.format(d="2.5 6.2 L5 8.6 L9.6 3.6", c=t["on_accent"]),
    }
    for name, svg in files.items():
        (_icon_dir / f"{_mode}-{name}.svg").write_text(svg)
    return _icon_dir


GRAIN_TILE = 128  # px; big enough that the repeat doesn't show, small enough to cost nothing


def _grain(t: dict) -> Path:
    """Write a tileable noise texture for the glass panels, tuned per theme.

    Each pixel is a light or dark speck at a random, mostly tiny alpha, so the
    texture reads as grain rather than as a tint and leaves text contrast alone.
    Painted once per theme and tiled by the stylesheet, so it's cheap to draw.
    """
    key = "".join(f"{v:02x}" for c in t["grain"] for v in c)  # retuned values get a fresh file
    path = _icons(t) / f"{_mode}-grain-{key}.png"
    if path.exists():
        return path
    (dr, dg, db, da), (lr, lg, lb, la) = t["grain"]
    rng = random.Random(7)  # fixed seed: the same grain every run
    img = QImage(GRAIN_TILE, GRAIN_TILE, QImage.Format.Format_ARGB32)
    for y in range(GRAIN_TILE):
        for x in range(GRAIN_TILE):
            v = rng.random()
            strength = rng.random() ** 2  # most specks faint, a few stronger
            if v < 0.5:
                img.setPixelColor(x, y, QColor(dr, dg, db, int(da * strength)))
            else:
                img.setPixelColor(x, y, QColor(lr, lg, lb, int(la * strength)))
    img.save(str(path))
    return path


def stylesheet(t: dict) -> str:
    icons = _icons(t).as_posix()  # forward slashes; urls are quoted below for paths with spaces
    glass, strong, soft = _rgba(t["glass"]), _rgba(t["glass_strong"]), _rgba(t["glass_soft"])
    solid = _rgba(t["solid"])
    display = f'"{DISPLAY_FAMILY}"'
    hi, edge, sel = _rgba(t["edge_hi"]), _rgba(t["edge"]), _rgba(t["selection"])
    grain = "none" if _reduce else f'url("{_grain(t).as_posix()}")'
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {t['text']}; background: transparent; }}
QMainWindow, QDialog {{ background: {t['bg0']}; }}
QToolTip {{ background: {t['menu']}; color: {t['text']}; border: 1px solid {edge}; border-radius: 8px; padding: 6px 8px; }}

/* Panels */
QFrame[glass="true"] {{
    background-color: {glass}; background-image: {grain};
    border: 1px solid {edge}; border-top-color: {hi}; border-left-color: {hi};
    border-radius: 18px;
}}
QFrame[glass="tile"] {{
    background-color: {strong}; background-image: {grain};
    border: 1px solid {edge}; border-top-color: {hi}; border-left-color: {hi};
    border-radius: 16px;
}}
QFrame[glass="solid"] {{
    background: {solid};
    border: 1px solid {edge}; border-top-color: {hi}; border-left-color: {hi};
    border-radius: 18px;
}}
QLabel {{ background: transparent; border: none; }}
QLabel[role="title"] {{ font-family: {display}; font-size: 16pt; font-weight: 650; letter-spacing: -0.4px; }}
QLabel[role="subtitle"] {{ color: {t['muted']}; }}
QLabel[role="section"] {{ color: {t['muted']}; font-size: 8.5pt; font-weight: 600; letter-spacing: 1.2px; }}
QLabel[role="heading"] {{ font-family: {display}; font-size: 15pt; font-weight: 650; letter-spacing: -0.2px; }}
QLabel[role="value"] {{ font-family: {display}; font-size: 21pt; font-weight: 650; letter-spacing: -0.5px; }}
QLabel[role="caption"], QLabel[role="muted"] {{ color: {t['muted']}; }}
QLabel[role="status"] {{ color: {t['muted']}; padding: 2px 6px; }}

/* Inputs */
QLineEdit, QSpinBox, QComboBox, QDateEdit {{
    background: {strong}; border: 1px solid {edge}; border-radius: 10px;
    padding: 5px 10px; min-height: 20px; max-height: 20px; selection-background-color: {t['accent']}; selection-color: {t['on_accent']};
}}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QDateEdit:hover {{ border-color: {t['faint']}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QComboBox:on, QDateEdit:focus {{ border: 1px solid {t['accent']}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled, QDateEdit:disabled {{ color: {t['faint']}; background: {soft}; }}
QLineEdit[placeholderText] {{ }}
QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 26px; background: transparent; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url("{icons}/{_mode}-down.svg"); width: 12px; height: 12px; }}
QDateEdit {{ padding-right: 26px; }}
QCalendarWidget QWidget {{ background: {t['menu']}; color: {t['text']}; alternate-background-color: {t['menu']}; }}
QCalendarWidget QAbstractItemView {{ selection-background-color: {t['accent']}; selection-color: {t['on_accent']}; }}
QCalendarWidget QAbstractItemView:disabled {{ color: {t['faint']}; }}
QCalendarWidget QToolButton {{ background: transparent; color: {t['text']}; border: none; padding: 4px 8px; font-weight: 600; }}
QComboBox QAbstractItemView {{
    background: {t['menu']}; border: 1px solid {edge}; border-radius: 10px; padding: 4px;
    selection-background-color: {sel}; selection-color: {t['text']}; outline: 0;
}}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 22px; background: transparent; subcontrol-origin: border; }}
QSpinBox::up-button {{ subcontrol-position: top right; margin: 3px 4px 0 0; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 4px 3px 0; }}
QSpinBox::up-arrow {{ image: url("{icons}/{_mode}-up.svg"); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url("{icons}/{_mode}-down.svg"); width: 10px; height: 10px; }}

QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {t['faint']}; background: {strong}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url("{icons}/{_mode}-check.svg"); }}
QCheckBox:disabled {{ color: {t['faint']}; }}

/* Buttons */
QPushButton {{
    background: {strong}; border: 1px solid {edge}; border-top-color: {hi};
    border-radius: 11px; padding: 8px 16px; font-weight: 600;
}}
QPushButton:hover {{ border-color: {t['faint']}; }}
QPushButton:pressed {{ background: {glass}; }}
QPushButton:disabled {{ color: {t['faint']}; background: {soft}; border-color: {edge}; }}
QPushButton:focus {{ border-color: {t['accent']}; }}
QPushButton:checked {{ background: {sel}; border-color: {t['accent']}; }}  /* a toggle button that is on */
QPushButton[primary="true"] {{
    color: {t['on_accent']}; border: 1px solid rgba(255, 255, 255, 60);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['button0']}, stop:1 {t['button1']});
}}
QPushButton[primary="true"]:hover {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {QColor(t['button0']).lighter(112).name()}, stop:1 {QColor(t['button1']).lighter(112).name()});
}}
QPushButton[primary="true"]:disabled {{ color: rgba(255, 255, 255, 170);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {t['faint']}, stop:1 {t['faint']}); }}
QPushButton[flat="true"] {{ background: transparent; border: 1px solid transparent; color: {t['muted']}; }}
QPushButton[flat="true"]:hover {{ background: {soft}; border-color: {edge}; color: {t['text']}; }}
QPushButton[small="true"] {{ padding: 3px 9px; border-radius: 8px; font-size: 9pt; }}

QProgressBar {{ background: {soft}; border: none; border-radius: 3px; max-height: 6px; min-height: 6px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ border-radius: 3px; background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {t['accent']}, stop:1 {t['accent2']}); }}

/* Tabs as a segmented control */
QTabWidget::pane {{ border: none; top: 0; }}
QTabWidget::tab-bar {{ left: 0; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {t['muted']}; border: 1px solid transparent; border-radius: 10px;
    padding: 7px 16px; margin: 0 4px 0 0; font-weight: 600;
}}
QTabBar::tab:hover {{ color: {t['text']}; background: {soft}; }}
QTabBar::tab:selected {{ color: {t['text']}; background: {strong}; border-color: {edge}; border-top-color: {hi}; }}

/* Tables and lists */
QTableView, QListWidget {{
    background: transparent; border: none; gridline-color: transparent;
    selection-background-color: {sel}; selection-color: {t['text']}; alternate-background-color: {soft};
}}
QTableView::item {{ padding: 0 8px; border: none; }}
QListWidget::item {{ padding: 7px 8px; border-radius: 8px; margin: 1px 0; }}
QListWidget::item:hover, QTableView::item:hover {{ background: {soft}; }}
QListWidget::item:selected {{ background: {sel}; color: {t['text']}; }}
QHeaderView {{ background: transparent; border: none; }}
QHeaderView::section {{
    background: transparent; color: {t['muted']}; border: none; border-bottom: 1px solid {edge};
    padding: 8px; font-weight: 600; font-size: 8.5pt;
}}
QHeaderView::section:hover {{ color: {t['text']}; }}
QHeaderView::up-arrow {{ image: url("{icons}/{_mode}-up.svg"); width: 10px; height: 10px; subcontrol-position: center right; padding-right: 6px; }}
QHeaderView::down-arrow {{ image: url("{icons}/{_mode}-down.svg"); width: 10px; height: 10px; subcontrol-position: center right; padding-right: 6px; }}
QTableCornerButton::section {{ background: transparent; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle {{ background: {_rgba((*QColor(t['faint']).getRgb()[:3], 110))}; border-radius: 3px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {t['faint']}; }}
QScrollBar[quiet="true"][hot="false"]::handle {{ background: transparent; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QSplitter::handle {{ background: transparent; }}

QMenuBar {{ background: transparent; padding: 4px 8px; }}
QMenuBar::item {{ background: transparent; padding: 5px 10px; border-radius: 8px; }}
QMenuBar::item:selected {{ background: {soft}; }}
QMenu {{ background: {t['menu']}; border: 1px solid {edge}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {sel}; }}
QMenu::item:disabled {{ color: {t['faint']}; }}
QMenu::separator {{ height: 1px; background: {edge}; margin: 5px 8px; }}
QMenu::indicator {{ width: 14px; height: 14px; }}
QStatusBar {{ background: transparent; color: {t['muted']}; }}
QStatusBar::item {{ border: none; }}
QMessageBox, QFileDialog {{ background: {t['menu']}; }}
"""


def apply(app: QApplication, requested: str = "system", reduce_transparency: bool = False) -> None:
    """requested is "system", "light" or "dark"."""
    global _mode, _reduce
    _mode = system_mode() if requested == "system" else requested
    _reduce = reduce_transparency
    t = tokens()
    app.setStyle("Fusion")
    pal = QPalette()
    for role, color in [
        (QPalette.ColorRole.Window, t["bg0"]), (QPalette.ColorRole.WindowText, t["text"]),
        (QPalette.ColorRole.Base, t["menu"]), (QPalette.ColorRole.AlternateBase, t["bg1"]),
        (QPalette.ColorRole.Text, t["text"]), (QPalette.ColorRole.Button, t["menu"]),
        (QPalette.ColorRole.ButtonText, t["text"]), (QPalette.ColorRole.Highlight, t["accent"]),
        (QPalette.ColorRole.HighlightedText, t["on_accent"]), (QPalette.ColorRole.ToolTipBase, t["menu"]),
        (QPalette.ColorRole.ToolTipText, t["text"]), (QPalette.ColorRole.PlaceholderText, t["faint"]),
        (QPalette.ColorRole.Link, t["accent"]),
    ]:
        pal.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["faint"]))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(t))


def mark(widget: QWidget, prop: str, value) -> QWidget:
    widget.setProperty(prop, value)
    return widget


def mood_score(positive: int, negative: int, total: int) -> float:
    """Net share of positive over negative items, scaled to -1..1 (±50 points of net share is the full tint)."""
    if not total:
        return 0.0
    return max(-1.0, min(1.0, 2.0 * (positive - negative) / total))


def _mix(a: QColor, b: QColor, f: float) -> QColor:
    return QColor.fromRgbF(*(x + (y - x) * f for x, y in zip(a.getRgbF()[:3], b.getRgbF()[:3])), a.alphaF())


def mood_color(score: float) -> tuple[QColor, float]:
    """The wash for a mood score in -1..1: green, white or red (alpha carries its strength), and its weight 0..1."""
    m = MOOD[_mode]
    white, wa = m["neutral"]
    far, fa = m["positive" if score >= 0 else "negative"]
    f = abs(score)
    c = _mix(QColor(*white), QColor(*far), f)
    c.setAlpha(round(wa + (fa - wa) * f))
    return c, f


class MoodTint(QObject):
    """Eases the backdrop's mood between datasets and repaints the window as it goes.

    State is a point (score, presence): presence fades the wash in and out, so "no data" and
    "tint off" look exactly like the plain backdrop.
    """

    def __init__(self, window: QWidget, enabled: bool = True):
        super().__init__(window)
        self._window = window
        self.enabled = enabled
        self._target = QPointF(0, 0)
        self.state = QPointF(0, 0)
        self._anim = QVariantAnimation(self, duration=900, easingCurve=QEasingCurve.Type.InOutCubic)
        self._anim.valueChanged.connect(self._step)

    def set_counts(self, positive: int, negative: int, total: int) -> None:
        self._target = QPointF(mood_score(positive, negative, total), 1.0 if total else 0.0)
        self._go()

    def set_enabled(self, on: bool) -> None:
        self.enabled = on
        self._go()

    def _go(self) -> None:
        target = self._target if self.enabled else QPointF(self.state.x(), 0.0)
        self._anim.stop()
        if not self._window.isVisible():  # nothing to watch yet: jump straight there
            self._step(target)
            return
        self._anim.setStartValue(QPointF(self.state))
        self._anim.setEndValue(target)
        self._anim.start()

    def _step(self, value: QPointF) -> None:
        self.state = QPointF(value)
        self._window.update()


def paint_backdrop(widget: QWidget, over_native: bool = False, mood: QPointF | None = None) -> None:
    """Window background: a diagonal gradient with a few large, soft color glows.

    over_native: a native material (Windows Mica/Acrylic, macOS vibrancy) is behind the window, so only
    lay a thin tint over it (enough to keep text contrast) and fainter glows.
    mood: MoodTint.state, (score -1..1, presence 0..1); leans the glows and a diagonal wash toward its color.
    """
    t = tokens()
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r = QRectF(widget.rect())
    base = QLinearGradient(r.topLeft(), r.bottomRight())
    c0, c1 = QColor(t["bg0"]), QColor(t["bg1"])
    if over_native:
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        c0.setAlpha(150 if _mode == "dark" else 120)
        c1.setAlpha(150 if _mode == "dark" else 120)
    base.setColorAt(0, c0)
    base.setColorAt(1, c1)
    p.fillRect(r, base)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    w, h = r.width(), r.height()
    presence = mood.y() if mood is not None else 0.0
    wash, weight = mood_color(mood.x()) if presence > 0 else (QColor(0, 0, 0, 0), 0.0)
    # Glows lean further toward green/red than toward white, which would just wash them out.
    lean = MOOD[_mode]["blob_mix"] * presence * (0.35 + 0.65 * weight)
    spots = [(0.12, 0.08, 0.55), (0.92, 0.18, 0.5), (0.62, 1.0, 0.6)]
    for (fx, fy, fr), (hex_color, alpha) in zip(spots, t["blobs"]):
        radius = max(w, h) * fr
        g = QRadialGradient(QPointF(w * fx, h * fy), radius)
        c = _mix(QColor(hex_color), wash, lean) if presence > 0 else QColor(hex_color)
        c.setAlpha(alpha // 2 if over_native else alpha)
        g.setColorAt(0, c)
        c.setAlpha(0)
        g.setColorAt(1, c)
        p.fillRect(r, g)
    if presence > 0:
        # Strongest at the top left (where the eye lands first), fading to a trace at the far corner.
        a = wash.alphaF() * presence * (0.5 if over_native else 1.0)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        for at, k in ((0.0, 1.0), (0.55, 0.7), (1.0, 0.35)):
            c = QColor(wash)
            c.setAlphaF(a * k)
            g.setColorAt(at, c)
        p.fillRect(r, g)
