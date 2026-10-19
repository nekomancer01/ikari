import os
import subprocess
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from subsentiment.gui import native  # noqa: E402
from subsentiment.gui.native import linux, macos, windows  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def keys(options):
    return [o.key for o in options]


def test_colorref_is_bgr():
    assert windows._colorref("#112233") == 0x332211


def test_windows_module_noop_off_windows():
    if sys.platform == "win32":
        return
    assert not windows.backdrop_supported()
    assert windows.apply_backdrop(0, "acrylic") is False
    windows.style_title_bar(0, dark=True, caption="#000000", text="#ffffff")  # must not raise
    windows.set_app_user_model_id()


def test_macos_module_noop_off_macos(qapp):
    if sys.platform == "darwin":
        return
    w = QMainWindow()
    assert not macos.available()
    assert macos.apply_backdrop(w, "vibrancy") is False
    macos.prepare_window(w)
    macos.set_appearance("dark")
    macos.set_process_name()


@pytest.mark.parametrize("plat, name", [("win32", "windows"), ("darwin", "macos"), ("linux", "linux"),
                                        ("freebsd14", "linux")])
def test_platform_name(monkeypatch, plat, name):
    monkeypatch.setattr(sys, "platform", plat)
    assert native.platform_name() == name


def test_linux_offers_only_gradient(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert keys(native.backdrop_options()) == ["gradient"]
    assert native.resolve_backdrop("mica") is None
    assert native.resolve_backdrop("vibrancy") is None


def test_windows_options(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows, "backdrop_supported", lambda: False)
    opts = native.backdrop_options()
    assert keys(opts) == ["gradient", "mica", "acrylic"]
    assert opts[1].unavailable and opts[2].unavailable
    assert native.resolve_backdrop("acrylic") is None
    monkeypatch.setattr(windows, "backdrop_supported", lambda: True)
    assert all(o.unavailable is None for o in native.backdrop_options())
    assert native.resolve_backdrop("acrylic") == "acrylic"
    assert native.resolve_backdrop("vibrancy") is None
    assert native.resolve_backdrop("gradient") is None


def test_macos_options(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos, "available", lambda: False)
    opts = native.backdrop_options()
    assert keys(opts) == ["gradient", "vibrancy"]
    assert opts[1].unavailable
    assert native.resolve_backdrop("vibrancy") is None
    monkeypatch.setattr(macos, "available", lambda: True)
    assert native.resolve_backdrop("vibrancy") == "vibrancy"
    assert native.resolve_backdrop("mica") is None


def test_prepare_window_sets_translucency_only_for_native(qapp, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(macos, "available", lambda: True)
    w = QMainWindow()
    assert native.prepare_window(w, "vibrancy") == "vibrancy"
    assert w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    w2 = QMainWindow()
    assert native.prepare_window(w2, "gradient") is None
    assert not w2.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)


@pytest.mark.parametrize("plat", ["win32", "darwin", "linux"])
def test_facade_is_safe_on_this_machine(qapp, monkeypatch, plat):
    """Pretending to be another OS must never reach a real OS API or raise."""
    monkeypatch.setattr(sys, "platform", plat)
    w = QMainWindow()
    kind = native.prepare_window(w, "acrylic" if plat == "win32" else "vibrancy")
    if sys.platform not in ("win32", "darwin"):
        assert kind is None
        assert native.apply_backdrop(w, "acrylic") is False
        assert native.apply_backdrop(w, "vibrancy") is False
    native.style_title_bar(w, dark=True, caption="#000000", text="#ffffff")
    native.style_title_bar(w, dark=False)
    native.set_appearance("dark")
    native.set_appearance("system")
    if plat != "linux":
        native.setup_app_identity()


def test_linux_identity(qapp, monkeypatch):
    from PySide6.QtGui import QGuiApplication
    monkeypatch.setattr(sys, "platform", "linux")
    native.setup_app_identity()
    assert QGuiApplication.desktopFileName() == "io.github.nekomancer01.ikari"


def test_fallback_color_scheme_only_on_linux(monkeypatch):
    monkeypatch.setattr(linux, "color_scheme", lambda: "dark")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert native.fallback_color_scheme() is None
    monkeypatch.setattr(sys, "platform", "linux")
    assert native.fallback_color_scheme() == "dark"


class _Run:
    def __init__(self, answers):
        self.answers = answers

    def __call__(self, cmd, **kwargs):
        assert kwargs.get("timeout")  # never block startup on a hung settings daemon
        out = self.answers.get(cmd[-1])
        if isinstance(out, Exception):
            raise out
        return subprocess.CompletedProcess(cmd, 0 if out is not None else 1, stdout=out or "", stderr="")


@pytest.mark.parametrize("answers, expected", [
    ({"color-scheme": "'prefer-dark'\n"}, "dark"),
    ({"color-scheme": "'prefer-light'\n"}, "light"),
    ({"color-scheme": "'default'\n", "gtk-theme": "'Adwaita-dark'\n"}, "dark"),
    ({"color-scheme": "'default'\n", "gtk-theme": "'Adwaita'\n"}, "light"),
    ({}, None),
    ({"color-scheme": subprocess.TimeoutExpired("gsettings", 0.5)}, None),
])
def test_linux_color_scheme(monkeypatch, answers, expected):
    monkeypatch.delenv("GTK_THEME", raising=False)
    monkeypatch.setattr(linux.shutil, "which", lambda _: "/usr/bin/gsettings")
    monkeypatch.setattr(linux.subprocess, "run", _Run(answers))
    assert linux.color_scheme() == expected


def test_linux_color_scheme_env_and_missing_tool(monkeypatch):
    monkeypatch.setattr(linux.shutil, "which", lambda _: None)
    monkeypatch.setenv("GTK_THEME", "Adwaita:dark")
    assert linux.color_scheme() == "dark"
    monkeypatch.delenv("GTK_THEME")
    assert linux.color_scheme() is None
