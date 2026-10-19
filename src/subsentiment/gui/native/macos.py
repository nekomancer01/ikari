"""macOS integration. Every function is a no-op elsewhere, and every failure is a no-op too.

- A unified title bar; with vibrancy on, a full-size one so the blur runs under
  the traffic lights (Qt 6.9+ window hints; older Qt keeps the standard title bar).
- Light/dark appearance of the title bar and native dialogs follows the app
  theme, not only the system.
- An optional "vibrancy" backdrop: an NSVisualEffectView behind the Qt
  content, through pyobjc (pyobjc-framework-Cocoa). Like Windows Mica, the
  window must have Qt.WA_TranslucentBackground set before it is first shown.
- The app menu reads "Subreddit Sentiment" instead of "Python" when run from source.
"""
from __future__ import annotations

import sys

IS_MACOS = sys.platform == "darwin"
APP_NAME = "Subreddit Sentiment"
BACKDROPS = ("vibrancy",)

# AppKit constants, spelled out so nothing needs importing at module level.
_MATERIAL_UNDER_WINDOW = 21  # NSVisualEffectMaterialUnderWindowBackground (10.14+)
_BLENDING_BEHIND_WINDOW = 0  # NSVisualEffectBlendingModeBehindWindow
_STATE_ACTIVE = 1  # NSVisualEffectStateActive: stay frosted when the window is inactive
_SIZABLE = 2 | 16  # NSViewWidthSizable | NSViewHeightSizable
_BELOW = -1  # NSWindowBelow


def _appkit():
    """The AppKit module from pyobjc, or None off macOS or when pyobjc is missing."""
    if not IS_MACOS:
        return None
    try:
        import AppKit
        return AppKit
    except Exception:
        return None


def available() -> bool:
    """True when the vibrancy backdrop can be offered (macOS with pyobjc)."""
    return _appkit() is not None


def set_process_name() -> None:
    """Name the app menu after the app when running unbundled. Call before QApplication."""
    if _appkit() is None:
        return
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        info = bundle.localizedInfoDictionary() or bundle.infoDictionary()
        if info is not None and info.get("CFBundleName") in (None, "Python", "python"):
            info["CFBundleName"] = APP_NAME
    except Exception:
        pass


def prepare_window(window, full_size: bool = False) -> None:
    """Before first show: unified title bar; with full_size, content runs under the title bar.

    full_size is only used with vibrancy, so the default window keeps the standard
    title bar until the Qt 6.9 safe-area handling has been checked on a real Mac.
    """
    if not IS_MACOS:
        return
    try:
        from PySide6.QtCore import Qt
        window.setUnifiedTitleAndToolBarOnMac(True)
        hints = [getattr(Qt.WindowType, n, None) for n in ("ExpandedClientAreaHint", "NoTitleBarBackgroundHint")]
        if full_size and None not in hints:  # Qt 6.9+: layouts keep clear of the title bar through the safe-area margins
            for hint in hints:
                window.setWindowFlag(hint, True)
    except Exception:
        pass


def set_appearance(requested: str) -> None:
    """Force Aqua or Dark Aqua for "light"/"dark"; "system" follows macOS again."""
    if not IS_MACOS:
        return
    try:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QGuiApplication
        hints = QGuiApplication.styleHints()
        if hasattr(hints, "setColorScheme"):  # Qt 6.8+ sets NSApp.appearance itself
            hints.setColorScheme({"dark": Qt.ColorScheme.Dark, "light": Qt.ColorScheme.Light}
                                 .get(requested, Qt.ColorScheme.Unknown))
            return
    except Exception:
        return
    AppKit = _appkit()
    if AppKit is None:
        return
    try:
        name = {"dark": AppKit.NSAppearanceNameDarkAqua, "light": AppKit.NSAppearanceNameAqua}.get(requested)
        appearance = AppKit.NSAppearance.appearanceNamed_(name) if name else None
        AppKit.NSApplication.sharedApplication().setAppearance_(appearance)
    except Exception:
        pass


def apply_backdrop(window, kind: str) -> bool:
    """After show: put a behind-window NSVisualEffectView under the Qt view. False if it failed."""
    AppKit = _appkit()
    if AppKit is None or kind not in BACKDROPS:
        return False
    try:
        import objc
        view = objc.objc_object(c_void_p=int(window.winId()))  # winId() is the QNSView
        ns_window, frame_view = view.window(), view.superview()
        if ns_window is None or frame_view is None:
            return False
        effect = AppKit.NSVisualEffectView.alloc().initWithFrame_(frame_view.bounds())
        effect.setAutoresizingMask_(_SIZABLE)
        effect.setMaterial_(_MATERIAL_UNDER_WINDOW)
        effect.setBlendingMode_(_BLENDING_BEHIND_WINDOW)
        effect.setState_(_STATE_ACTIVE)
        frame_view.addSubview_positioned_relativeTo_(effect, _BELOW, view)
        ns_window.setOpaque_(False)
        ns_window.setBackgroundColor_(AppKit.NSColor.clearColor())
        ns_window.setTitlebarAppearsTransparent_(True)
        return True
    except Exception:
        return False
