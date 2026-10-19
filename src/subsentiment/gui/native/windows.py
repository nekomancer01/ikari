"""Windows 10/11 integration through the DWM API. Every function is a no-op elsewhere.

Use it through the platform-neutral facade in subsentiment.gui.native.

- A title bar that matches the app theme (dark mode flag, plus caption colors on Windows 11).
- An optional system backdrop material (Mica or Acrylic) on Windows 11 22H2+
  (build 22523+, the documented DWMWA_SYSTEMBACKDROP_TYPE attribute). The window
  must have Qt.WA_TranslucentBackground set before it is first shown.
- An explicit AppUserModelID so the taskbar groups the app under its own icon
  instead of python.exe.
"""
from __future__ import annotations

import ctypes
import sys

IS_WINDOWS = sys.platform == "win32"

DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1 = 19  # Windows 10 1809 to 1909 know only this id
DWMWA_BORDER_COLOR = 34
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38
DWMWA_COLOR_DEFAULT = 0xFFFFFFFF
BACKDROPS = {"mica": 2, "acrylic": 3}

APP_ID = "nekomancer01.ikari"


def windows_build() -> int:
    return sys.getwindowsversion().build if IS_WINDOWS else 0


def backdrop_supported() -> bool:
    return windows_build() >= 22523


class _MARGINS(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("right", ctypes.c_int), ("top", ctypes.c_int), ("bottom", ctypes.c_int)]


def _set_attr(hwnd: int, attr: int, value: int) -> bool:
    data = ctypes.c_uint(value)
    result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
        ctypes.c_void_p(hwnd), ctypes.c_uint(attr), ctypes.byref(data), ctypes.sizeof(data))
    return result == 0  # S_OK


def _colorref(hex_color: str) -> int:
    """'#rrggbb' -> COLORREF (0x00bbggrr)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return (b << 16) | (g << 8) | r


def set_app_user_model_id() -> None:
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_ID))
    except (AttributeError, OSError):
        pass


def style_title_bar(hwnd: int, dark: bool, caption: str | None = None, text: str | None = None) -> None:
    """Dark/light title bar; on Windows 11 also tint it to match the window. caption=None restores default."""
    if not IS_WINDOWS:
        return
    try:
        flag = 1 if dark else 0
        if not _set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, flag):
            _set_attr(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE_PRE_20H1, flag)
        if windows_build() >= 22000:
            _set_attr(hwnd, DWMWA_CAPTION_COLOR, _colorref(caption) if caption else DWMWA_COLOR_DEFAULT)
            _set_attr(hwnd, DWMWA_TEXT_COLOR, _colorref(text) if text else DWMWA_COLOR_DEFAULT)
    except (AttributeError, OSError):
        pass


def apply_backdrop(hwnd: int, kind: str) -> bool:
    """Turn on Mica or Acrylic behind the whole window. Returns False if unsupported or it failed."""
    if not backdrop_supported() or kind not in BACKDROPS:
        return False
    try:
        margins = _MARGINS(-1, -1, -1, -1)  # extend the frame over the whole client area
        ctypes.windll.dwmapi.DwmExtendFrameIntoClientArea(ctypes.c_void_p(hwnd), ctypes.byref(margins))
        return _set_attr(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, BACKDROPS[kind])
    except (AttributeError, OSError):
        return False
