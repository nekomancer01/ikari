"""The ctypes DWM calls, run against a fake ``ctypes.windll`` so the arguments can be checked on any OS."""
import ctypes
import sys
from types import SimpleNamespace

import pytest

from subsentiment.gui import native
from subsentiment.gui.native import windows


class FakeDwm:
    def __init__(self, result=0, fail_attrs=()):
        self.result = result
        self.fail_attrs = set(fail_attrs)
        self.attrs = []  # (hwnd, attr, value, size)
        self.extended = []

    def DwmSetWindowAttribute(self, hwnd, attr, ref, size):
        attr_id = attr.value
        value = ref._obj.value
        self.attrs.append((hwnd.value, attr_id, value, size))
        return 1 if attr_id in self.fail_attrs else self.result

    def DwmExtendFrameIntoClientArea(self, hwnd, ref):
        m = ref._obj
        self.extended.append((hwnd.value, m.left, m.right, m.top, m.bottom))
        return self.result


@pytest.fixture
def dwm(monkeypatch):
    fake = FakeDwm()
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(dwmapi=fake), raising=False)
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "windows_build", lambda: 22631)
    return fake


def test_dark_title_bar_sets_flag_and_colors(dwm):
    windows.style_title_bar(1234, dark=True, caption="#102030", text="#ffffff")
    assert dwm.attrs == [
        (1234, windows.DWMWA_USE_IMMERSIVE_DARK_MODE, 1, 4),
        (1234, windows.DWMWA_CAPTION_COLOR, 0x302010, 4),
        (1234, windows.DWMWA_TEXT_COLOR, 0xFFFFFF, 4),
    ]


def test_title_bar_without_colors_restores_defaults(dwm):
    windows.style_title_bar(1, dark=False)
    values = {attr: value for _, attr, value, _ in dwm.attrs}
    assert values[windows.DWMWA_USE_IMMERSIVE_DARK_MODE] == 0
    assert values[windows.DWMWA_CAPTION_COLOR] == windows.DWMWA_COLOR_DEFAULT
    assert values[windows.DWMWA_TEXT_COLOR] == windows.DWMWA_COLOR_DEFAULT


def test_windows_10_skips_caption_colors_but_keeps_dark_mode(dwm, monkeypatch):
    monkeypatch.setattr(windows, "windows_build", lambda: 19045)
    windows.style_title_bar(1, dark=True, caption="#000000", text="#ffffff")
    assert [a for _, a, _, _ in dwm.attrs] == [windows.DWMWA_USE_IMMERSIVE_DARK_MODE]


def test_dark_mode_falls_back_to_the_pre_20h1_attribute(dwm, monkeypatch):
    """Windows 10 1809-1909 only know attribute 19; 20 returns an error there."""
    monkeypatch.setattr(windows, "windows_build", lambda: 18363)
    dwm.fail_attrs = {20}
    windows.style_title_bar(1, dark=True)
    assert [(a, v) for _, a, v, _ in dwm.attrs] == [(20, 1), (19, 1)]


@pytest.mark.parametrize("kind, expected", [("mica", 2), ("acrylic", 3)])
def test_backdrop_extends_frame_and_sets_material(dwm, kind, expected):
    assert windows.apply_backdrop(77, kind) is True
    assert dwm.extended == [(77, -1, -1, -1, -1)]
    assert dwm.attrs == [(77, windows.DWMWA_SYSTEMBACKDROP_TYPE, expected, 4)]


def test_backdrop_reports_failure_when_dwm_refuses(dwm):
    dwm.result = 0x80070057
    assert windows.apply_backdrop(77, "mica") is False


def test_backdrop_refused_on_old_windows_11_builds(dwm, monkeypatch):
    monkeypatch.setattr(windows, "windows_build", lambda: 22000)  # Windows 11 21H2: no system backdrop attribute
    assert windows.apply_backdrop(77, "mica") is False
    assert dwm.attrs == [] and dwm.extended == []


def test_backdrop_unknown_kind_is_rejected(dwm):
    assert windows.apply_backdrop(77, "vibrancy") is False
    assert dwm.attrs == []


def test_missing_dwmapi_is_swallowed(monkeypatch):
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    monkeypatch.setattr(windows, "windows_build", lambda: 22631)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(), raising=False)  # no .dwmapi -> AttributeError
    windows.style_title_bar(1, dark=True, caption="#000000")
    assert windows.apply_backdrop(1, "acrylic") is False


def test_app_user_model_id(monkeypatch):
    seen = []
    shell32 = SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=lambda s: seen.append(s.value))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(shell32=shell32), raising=False)
    monkeypatch.setattr(windows, "IS_WINDOWS", True)
    windows.set_app_user_model_id()
    assert seen == [windows.APP_ID]


def test_facade_passes_the_native_handle_to_dwm(dwm, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(windows, "backdrop_supported", lambda: True)
    window = SimpleNamespace(winId=lambda: 4242)
    assert native.apply_backdrop(window, "acrylic") is True
    native.style_title_bar(window, dark=True, caption="#000000", text="#ffffff")
    assert {a[0] for a in dwm.attrs} == {4242} and dwm.extended[0][0] == 4242
