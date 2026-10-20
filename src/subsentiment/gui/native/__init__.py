"""Platform-neutral native integration: the window code calls these, never an OS module directly.

Windows: Mica/Acrylic, tinted title bar, AppUserModelID (windows.py).
macOS: vibrancy, unified title bar, app appearance, app menu name (macos.py).
Linux: desktop file name, dark/light fallback (linux.py).
Every function is safe on every OS; anything unsupported is a no-op.
"""
from __future__ import annotations

import sys
from typing import NamedTuple

from PySide6.QtCore import Qt

from subsentiment.gui.native import linux, macos, windows

GRADIENT = "gradient"


class Backdrop(NamedTuple):
    key: str  # stored under the "ui/backdrop" setting
    label: str
    unavailable: str | None = None  # why it can't be used on this machine, None if it can


def platform_name() -> str:
    """"windows", "macos" or "linux" (any other Unix desktop counts as linux)."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def setup_app_identity() -> None:
    """Taskbar/dock/menu identity. Call before the QApplication is created."""
    name = platform_name()
    if name == "windows":
        windows.set_app_user_model_id()
    elif name == "macos":
        macos.set_process_name()
    else:
        linux.set_desktop_file_name()


def backdrop_options() -> list[Backdrop]:
    """Window backgrounds this OS offers; the painted gradient is always first and always works."""
    options = [Backdrop(GRADIENT, "Gradient")]
    name = platform_name()
    if name == "windows":
        why = None if windows.backdrop_supported() else "Needs Windows 11 version 22H2 or later"
        options += [Backdrop("mica", "Windows Mica", why), Backdrop("acrylic", "Windows Acrylic (most glassy)", why)]
    elif name == "macos":
        why = None if macos.available() else "Needs the pyobjc-framework-Cocoa package"
        options.append(Backdrop("vibrancy", "macOS vibrancy", why))
    return options


def resolve_backdrop(saved: str) -> str | None:
    """The native material to use for a saved setting, or None for the painted gradient."""
    return next((o.key for o in backdrop_options()
                 if o.key == saved and o.key != GRADIENT and o.unavailable is None), None)


def prepare_window(window, backdrop: str) -> str | None:
    """Before first show. Returns the native material that will be applied, or None."""
    kind = resolve_backdrop(backdrop)
    if kind:  # a native material needs a translucent window from the start
        window.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    if platform_name() == "macos":
        macos.prepare_window(window, full_size=kind is not None)
    return kind


def apply_backdrop(window, kind: str) -> bool:
    """After first show. False means fall back to the painted gradient."""
    name = platform_name()
    if name == "windows":
        return windows.apply_backdrop(int(window.winId()), kind)
    if name == "macos":
        return macos.apply_backdrop(window, kind)
    return False


def style_title_bar(window, dark: bool, caption: str | None = None, text: str | None = None) -> None:
    """Match the title bar to the theme; caption/text colors are used where the OS allows tinting."""
    if platform_name() == "windows":
        windows.style_title_bar(int(window.winId()), dark, caption=caption, text=text)


def set_appearance(requested: str) -> None:
    """Tell the OS the app's theme ("system", "light" or "dark"). Call before theme.apply."""
    if platform_name() == "macos":
        macos.set_appearance(requested)


def fallback_color_scheme() -> str | None:
    """"dark"/"light" from desktop settings when Qt can't tell, else None."""
    return linux.color_scheme() if platform_name() == "linux" else None


def animations_on() -> bool:
    """False when the OS asks apps to cut motion (Windows' Animation effects off)."""
    return windows.animations_on() if platform_name() == "windows" else True
