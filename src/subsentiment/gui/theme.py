"""Theme: a flat, light dashboard look. White panels with hairline borders sit on a soft gray window,
cards inside them are outlined rather than shaded, buttons and header pickers are pills, and the one
accent is a clear blue. Dark mode keeps the same structure on near-black surfaces.

The window background can carry the mood of the subreddit on screen: MoodTint pulls a soft wash toward
green when it skews positive, white when it's neutral and red when it skews negative, easing between
subreddits. It shows around the panels, so panel and text contrast hold.

Type: Inter (SIL Open Font License, bundled in assets/), medium for primary text and regular for secondary,
with its Display cut for headings and big numbers.
Tabular figures are switched on so numbers line up in columns.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QObject, QPointF, QRectF, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QFontDatabase, QGuiApplication, QLinearGradient, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QWidget

from subsentiment.gui import native

ASSETS = Path(__file__).parent / "assets"

THEMES = {
    "light": {
        "bg0": "#f1f1f1",
        # Panels and the cards on them are white; "soft" is the gray of a hover or a chosen row, "head" a table's.
        "panel": "#ffffff", "soft": "#f5f5f5", "head": "#f6f6f6",
        "edge": (0, 0, 0, 24),  # hairline borders and dividers, about #e8e8e8 on white
        "text": "#1f1f1f", "muted": "#737373", "faint": "#a3a3a3",
        "accent": "#0163ff", "on_accent": "#ffffff", "button": "#0163ff",
        "selection": (1, 99, 255, 26), "menu": "#ffffff", "shadow": (0, 0, 0, 40),
    },
    "dark": {
        "bg0": "#0e0e10",
        "panel": "#18181b", "soft": "#242428", "head": "#1f1f23",
        "edge": (255, 255, 255, 24),
        "text": "#ededef", "muted": "#9d9da3", "faint": "#6c6c72",
        "accent": "#3d86ff", "on_accent": "#ffffff",
        "button": "#2f74f0",  # a little deeper than the accent so white text keeps contrast
        "selection": (61, 134, 255, 52), "menu": "#1e1e22", "shadow": (0, 0, 0, 120),
    },
}

UI_FAMILIES = ["Inter", "Segoe UI Variable Text", "SF Pro Text", "Segoe UI", "Noto Sans"]
DISPLAY_FAMILY = "Inter Display"  # Inter cut for larger sizes: tighter spacing, finer details

# Sentiment wash over the window background: (rgb, peak alpha) per mood.
MOOD = {
    "light": {"positive": ((46, 196, 108), 70), "neutral": ((255, 255, 255), 120), "negative": ((236, 72, 72), 66)},
    "dark": {"positive": ((34, 170, 92), 60), "neutral": ((214, 222, 240), 22), "negative": ((214, 52, 60), 64)},
}

_mode = "light"
_reduce = False
_icon_dir: Path | None = None


def mode() -> str:
    return _mode


def tokens() -> dict:
    return THEMES[_mode]


def qt_args(argv: list[str]) -> list[str]:
    """Arguments for QApplication: on Windows, Qt's FreeType font engine instead of DirectWrite.

    DirectWrite ignores QFont.NoSubpixelAntialias, so text kept ClearType's blue and orange fringes, which
    look frayed at fractional scaling (tools/text_aa_check.py measures it). FreeType honours it. Left alone
    when a platform is already chosen on the command line or in QT_QPA_PLATFORM."""
    if sys.platform != "win32" or os.environ.get("QT_QPA_PLATFORM"):
        return argv
    if any(a == "-platform" or a.startswith("-platform=") for a in argv):
        return argv
    return [*argv, "-platform", "windows:fontengine=freetype"]


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
    f = QFont(QGuiApplication.font())  # the app font's features (tabular figures), so it measures as it paints
    f.setFamily(DISPLAY_FAMILY)
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
    # Static weights, not the variable font: Qt's variable-font weight
    # matching is unreliable across versions, static files always match.
    for path in sorted(ASSETS.glob("*.otf")):
        QFontDatabase.addApplicationFont(str(path))
    font = QFont()
    font.setFamilies(UI_FAMILIES)
    font.setPointSizeF(10)
    font.setWeight(QFont.Weight.Medium)  # primary text in medium, as in the reference; secondary text is regular
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
        "left": chevron.format(d="7.5 2.5 L4 6 L7.5 9.5", c=t["text"]),
        "right": chevron.format(d="4.5 2.5 L8 6 L4.5 9.5", c=t["text"]),
        "check": chevron.format(d="2.5 6.2 L5 8.6 L9.6 3.6", c=t["on_accent"]),
    }
    for name, svg in files.items():
        (_icon_dir / f"{_mode}-{name}.svg").write_text(svg)
    return _icon_dir


def stylesheet(t: dict) -> str:
    icons = _icons(t).as_posix()  # forward slashes; urls are quoted below for paths with spaces
    panel, soft, head = t["panel"], t["soft"], t["head"]
    display = f'"{DISPLAY_FAMILY}"'
    edge, sel = _rgba(t["edge"]), _rgba(t["selection"])
    return f"""
* {{ outline: 0; }}
QWidget {{ color: {t['text']}; background: transparent; }}
QMainWindow, QDialog {{ background: {t['bg0']}; }}
QToolTip {{ background: {t['menu']}; color: {t['text']}; border: 1px solid {edge}; border-radius: 8px; padding: 6px 8px; }}

/* Panels: "true" is a page-level panel (the sidebar, the main area), "tile" and "solid" the cards on one */
QFrame[glass="true"] {{ background: {panel}; border: 1px solid {edge}; border-radius: 16px; }}
QFrame[glass="tile"], QFrame[glass="solid"] {{ background: {panel}; border: 1px solid {edge}; border-radius: 10px; }}
QFrame[role="divider"] {{ background: {edge}; border: none; min-height: 1px; max-height: 1px; }}
QLabel {{ background: transparent; border: none; }}
QLabel[role="title"] {{ font-family: {display}; font-size: 13pt; font-weight: 600; letter-spacing: -0.2px; }}
QLabel[role="subtitle"] {{ color: {t['muted']}; font-weight: 400; }}
QLabel[role="section"] {{ color: {t['muted']}; font-size: 9pt; font-weight: 400; }}
QLabel[role="heading"] {{ font-family: {display}; font-size: 14pt; font-weight: 600; letter-spacing: -0.3px; }}
QLabel[role="value"] {{ font-family: {display}; font-size: 19pt; font-weight: 600; letter-spacing: -0.6px; }}
QLabel[role="caption"], QLabel[role="muted"] {{ color: {t['muted']}; font-weight: 400; }}
QLabel[role="status"] {{ color: {t['muted']}; padding: 2px 6px; }}

/* Inputs */
QLineEdit, QSpinBox, QComboBox, QDateEdit {{
    background: {panel}; border: 1px solid {edge}; border-radius: 8px;
    padding: 5px 10px; min-height: 20px; max-height: 20px; selection-background-color: {t['accent']}; selection-color: {t['on_accent']};
}}
QComboBox[pill="true"] {{ border-radius: 15px; padding: 5px 10px 5px 12px; font-weight: 500; }}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover, QDateEdit:hover {{ border-color: {t['faint']}; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QComboBox:on, QDateEdit:focus {{ border: 1px solid {t['accent']}; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled, QDateEdit:disabled {{ color: {t['faint']}; background: {soft}; }}
QPlainTextEdit {{
    background: {panel}; border: 1px solid {edge}; border-radius: 8px; padding: 4px 6px;
    selection-background-color: {t['accent']}; selection-color: {t['on_accent']};
}}
QPlainTextEdit:hover {{ border-color: {t['faint']}; }}
QPlainTextEdit:focus {{ border: 1px solid {t['accent']}; }}
QComboBox::drop-down, QDateEdit::drop-down {{ border: none; width: 26px; background: transparent; }}
QComboBox::down-arrow, QDateEdit::down-arrow {{ image: url("{icons}/{_mode}-down.svg"); width: 12px; height: 12px; }}
QDateEdit {{ padding-right: 26px; }}
/* The date fields' calendar (widgets.Calendar paints the card and the days) */
QCalendarWidget QWidget {{ background: transparent; color: {t['text']}; }}
QCalendarWidget QAbstractItemView {{ background: transparent; border: none; selection-background-color: transparent; }}
QCalendarWidget QAbstractItemView::item {{ border: none; padding: 0; }}
QCalendarWidget QToolButton {{
    background: transparent; color: {t['text']}; border: 1px solid transparent; border-radius: 8px;
    padding: 4px 6px; font-family: {display}; font-size: 11pt; font-weight: 600;
}}
QCalendarWidget QToolButton:hover {{ background: {soft}; }}
QCalendarWidget QToolButton:disabled {{ color: {t['faint']}; }}
QCalendarWidget QToolButton::menu-indicator {{ image: none; width: 0; }}
QCalendarWidget QToolButton#qt_calendar_prevmonth {{ qproperty-icon: url("{icons}/{_mode}-left.svg"); }}
QCalendarWidget QToolButton#qt_calendar_nextmonth {{ qproperty-icon: url("{icons}/{_mode}-right.svg"); }}
QCalendarWidget QSpinBox {{ font-family: {display}; font-size: 11pt; font-weight: 600; }}
QComboBox QAbstractItemView {{
    background: {t['menu']}; border: 1px solid {edge}; border-radius: 10px; padding: 4px;
    selection-background-color: {soft}; selection-color: {t['text']}; outline: 0;
}}
QSpinBox {{ padding-right: 24px; }}
QSpinBox::up-button, QSpinBox::down-button {{ border: none; width: 22px; background: transparent; subcontrol-origin: border; }}
QSpinBox::up-button {{ subcontrol-position: top right; margin: 3px 4px 0 0; }}
QSpinBox::down-button {{ subcontrol-position: bottom right; margin: 0 4px 3px 0; }}
QSpinBox::up-arrow {{ image: url("{icons}/{_mode}-up.svg"); width: 10px; height: 10px; }}
QSpinBox::down-arrow {{ image: url("{icons}/{_mode}-down.svg"); width: 10px; height: 10px; }}

QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{ width: 16px; height: 16px; border-radius: 5px; border: 1px solid {edge}; background: {panel}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url("{icons}/{_mode}-check.svg"); }}
QCheckBox:disabled {{ color: {t['faint']}; }}

/* Buttons: outlined pills, the main action filled with the accent */
QPushButton {{
    background: {panel}; border: 1px solid {edge}; border-radius: 14px; padding: 8px 16px; font-weight: 500;
}}
QPushButton:hover {{ background: {soft}; }}
QPushButton:pressed {{ background: {head}; }}
QPushButton:disabled {{ color: {t['faint']}; background: {panel}; border-color: {edge}; }}
QPushButton:focus {{ border-color: {t['accent']}; }}
QPushButton:checked {{ background: {sel}; border-color: {t['accent']}; }}  /* a toggle button that is on */
QPushButton[primary="true"] {{ color: {t['on_accent']}; border: 1px solid {t['button']}; background: {t['button']}; }}
QPushButton[primary="true"]:hover {{ background: {QColor(t['button']).darker(110).name()}; }}
QPushButton[primary="true"]:focus {{ border-color: {QColor(t['button']).darker(125).name()}; }}
QPushButton[primary="true"]:disabled {{ color: rgba(255, 255, 255, 190); background: {t['faint']}; border-color: {t['faint']}; }}
QPushButton[flat="true"] {{ background: transparent; border: 1px solid transparent; color: {t['muted']}; }}
QPushButton[flat="true"]:hover {{ background: {soft}; color: {t['text']}; }}
QPushButton[flat="true"]:checked {{ background: {panel}; border-color: {edge}; color: {t['text']}; }}  /* like a chosen tab */
QPushButton[flat="true"]:disabled {{ color: {t['faint']}; }}
QPushButton[small="true"] {{ padding: 3px 9px; border-radius: 8px; font-size: 9pt; }}

/* Tabs: plain words, the chosen one an outlined pill */
QTabWidget::pane {{ border: none; top: 0; }}
QTabWidget::tab-bar {{ left: 0; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{
    background: transparent; color: {t['muted']}; border: 1px solid transparent; border-radius: 15px;
    padding: 7px 14px; margin: 0 4px 0 0; font-weight: 500;
}}
QTabBar::tab:hover {{ color: {t['text']}; background: {soft}; }}
QTabBar::tab:selected {{ color: {t['text']}; background: {panel}; border-color: {edge}; }}

/* Tables and lists: a ruled table in an outlined box, its header row tinted */
QTableView {{
    background: {panel}; border: 1px solid {edge}; border-radius: 10px; gridline-color: transparent; font-weight: 400;
    selection-background-color: {sel}; selection-color: {t['text']}; alternate-background-color: {panel};
}}
QListWidget {{
    background: transparent; border: none;
    selection-background-color: {soft}; selection-color: {t['text']}; alternate-background-color: transparent;
}}
QTableView::item {{ padding: 0 5px; border: none; border-bottom: 1px solid {edge}; }}
QTableView::item:hover {{ background: {soft}; }}
QListWidget::item {{ padding: 7px 8px; border-radius: 8px; margin: 1px 0; }}
QListWidget::item:hover {{ background: {soft}; }}
QListWidget::item:selected {{ background: {soft}; color: {t['text']}; }}
QHeaderView {{ background: {head}; border: none; border-top-left-radius: 9px; border-top-right-radius: 9px; }}
QHeaderView::section {{
    background: {head}; color: {t['text']}; border: none; border-bottom: 1px solid {edge};
    padding: 9px 8px; font-weight: 500; font-size: 8.5pt;
}}
QHeaderView::section:first {{ border-top-left-radius: 9px; }}
QHeaderView::section:last {{ border-top-right-radius: 9px; }}
QHeaderView::section:hover {{ color: {t['accent']}; }}
QHeaderView::up-arrow {{ image: url("{icons}/{_mode}-up.svg"); width: 10px; height: 10px; subcontrol-position: center right; padding-right: 6px; }}
QHeaderView::down-arrow {{ image: url("{icons}/{_mode}-down.svg"); width: 10px; height: 10px; subcontrol-position: center right; padding-right: 6px; }}
QTableCornerButton::section {{ background: {head}; border: none; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 4px 2px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px 4px; }}
QScrollBar::handle {{ background: {_rgba((*QColor(t['faint']).getRgb()[:3], 110))}; border-radius: 3px; min-height: 30px; min-width: 30px; }}
QScrollBar::handle:hover {{ background: {t['faint']}; }}
QScrollBar[quiet="true"][hot="false"]::handle {{ background: transparent; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{ background: none; border: none; width: 0; height: 0; }}

QSplitter::handle {{ background: transparent; }}

QMenuBar {{ background: transparent; padding: 4px 8px; }}
QMenuBar::item {{ background: transparent; padding: 5px 10px; border-radius: 8px; }}
QMenuBar::item:selected {{ background: {_rgba(t['edge'])}; }}
QMenu {{ background: {t['menu']}; border: 1px solid {edge}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {soft}; }}
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
        (QPalette.ColorRole.Base, t["menu"]), (QPalette.ColorRole.AlternateBase, t["soft"]),
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


def paint_popup(widget: QWidget, shadow: int) -> None:
    """A popup's card: the menu color in a rounded hairline outline, inset by ``shadow`` px for a soft shadow
    around it. The popup window needs WA_TranslucentBackground so the corners and shadow show through."""
    t = tokens()
    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    card = QRectF(widget.rect()).adjusted(shadow, shadow, -shadow, -shadow)
    color = QColor(*t["shadow"][:3])
    for i in range(shadow, 0, -2):  # darker near the card and a little lower
        color.setAlpha(int(t["shadow"][3] * 0.16 * (1 - i / shadow) ** 1.5) + 1)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        p.drawRoundedRect(card.adjusted(-i, -i + 3, i, i + 3), 14 + i, 14 + i)
    p.setBrush(QColor(t["menu"]))
    p.setPen(QPen(QColor(*t["edge"][:3], min(255, t["edge"][3] * 2)), 1))
    p.drawRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)


def paint_backdrop(widget: QWidget, over_native: bool = False, mood: QPointF | None = None) -> None:
    """Window background: the theme's flat gray, with the mood wash on top.

    over_native: a native material (Windows Mica/Acrylic, macOS vibrancy) is behind the window, so only
    lay a thin tint over it; Reduce transparency paints the gray solid over it instead.
    mood: MoodTint.state, (score -1..1, presence 0..1); a diagonal wash in its color.
    """
    t = tokens()
    p = QPainter(widget)
    r = QRectF(widget.rect())
    base = QColor(t["bg0"])
    see_through = over_native and not _reduce
    if see_through:
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        base.setAlpha(150 if _mode == "dark" else 120)
    p.fillRect(r, base)
    p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
    presence = mood.y() if mood is not None else 0.0
    if presence > 0:
        wash, _ = mood_color(mood.x())
        # Strongest at the top left (where the eye lands first), fading to a trace at the far corner.
        a = wash.alphaF() * presence * (0.5 if see_through else 1.0)
        g = QLinearGradient(r.topLeft(), r.bottomRight())
        for at, k in ((0.0, 1.0), (0.55, 0.7), (1.0, 0.35)):
            c = QColor(wash)
            c.setAlphaF(a * k)
            g.setColorAt(at, c)
        p.fillRect(r, g)
