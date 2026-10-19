"""Linux (X11/Wayland) integration. Every function is safe to call on any OS.

- A desktop file name, so Wayland compositors and docks match windows to the
  installed .desktop entry (icon, name, grouping).
- A dark/light fallback for desktops where Qt reports no color scheme.
  Compositors vary too much for a native blur, so there is no backdrop material.
"""
from __future__ import annotations

import os
import shutil
import subprocess

DESKTOP_FILE = "io.github.subsentiment.SubredditSentiment"


def set_desktop_file_name() -> None:
    from PySide6.QtGui import QGuiApplication
    QGuiApplication.setDesktopFileName(DESKTOP_FILE)


def _gsettings(key: str) -> str:
    """Read an org.gnome.desktop.interface key, or "" if unavailable (short timeout, never raises)."""
    exe = shutil.which("gsettings")
    if not exe:
        return ""
    try:
        out = subprocess.run([exe, "get", "org.gnome.desktop.interface", key],
                             capture_output=True, text=True, timeout=0.5, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip().strip("'\"").lower() if out.returncode == 0 else ""


def color_scheme() -> str | None:
    """"dark" or "light" from the GTK_THEME env var or GNOME settings, None if nothing says."""
    gtk_env = os.environ.get("GTK_THEME", "").lower()
    if gtk_env:
        return "dark" if gtk_env.endswith(":dark") or "-dark" in gtk_env else "light"
    scheme = _gsettings("color-scheme")  # 'default', 'prefer-dark' or 'prefer-light' (GNOME 42+)
    if scheme == "prefer-dark":
        return "dark"
    if scheme == "prefer-light":
        return "light"
    theme_name = _gsettings("gtk-theme")
    if theme_name:
        return "dark" if "dark" in theme_name else "light"
    return None
