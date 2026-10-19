"""macOS code paths, run against stand-ins for pyobjc so the call sequence is checked on any OS."""
import os
import sys
from types import ModuleType, SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMainWindow  # noqa: E402

from subsentiment.gui import native  # noqa: E402
from subsentiment.gui.native import macos  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


class Recorder:
    def __init__(self, name, log):
        self._name, self._log = name, log

    def __getattr__(self, attr):
        def call(*args):
            self._log.append((self._name, attr, args))
            return None
        return call


class FakeView:
    def __init__(self, log, has_window=True, has_frame=True):
        self.log = log
        self.ns_window = Recorder("window", log) if has_window else None
        self.frame = SimpleNamespace(bounds=lambda: "bounds",
                                     addSubview_positioned_relativeTo_=lambda *a: log.append(("frame", "add", a))) \
            if has_frame else None

    def window(self):
        return self.ns_window

    def superview(self):
        return self.frame


@pytest.fixture
def fake_appkit(monkeypatch):
    log = []
    appkit = ModuleType("AppKit")

    class EffectView:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithFrame_(self, frame):
            log.append(("effect", "init", frame))
            return self

        def __getattr__(self, attr):
            return lambda *a: log.append(("effect", attr, a))

    appkit.NSVisualEffectView = EffectView
    appkit.NSColor = SimpleNamespace(clearColor=lambda: "clear")
    appkit.NSAppearanceNameDarkAqua, appkit.NSAppearanceNameAqua = "dark-aqua", "aqua"
    appkit.NSAppearance = SimpleNamespace(appearanceNamed_=lambda n: f"appearance:{n}")
    app = Recorder("app", log)
    appkit.NSApplication = SimpleNamespace(sharedApplication=lambda: app)
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setattr(macos, "IS_MACOS", True)
    return SimpleNamespace(log=log, module=appkit)


def install_objc(monkeypatch, view):
    objc = ModuleType("objc")
    objc.objc_object = lambda c_void_p: view
    monkeypatch.setitem(sys.modules, "objc", objc)


def test_available_follows_pyobjc(fake_appkit, monkeypatch):
    assert macos.available()
    monkeypatch.setitem(sys.modules, "AppKit", None)  # import raises ImportError
    assert not macos.available()


def test_vibrancy_inserts_effect_view_below_qt_view(fake_appkit, monkeypatch, qapp):
    view = FakeView(fake_appkit.log)
    install_objc(monkeypatch, view)
    w = QMainWindow()
    assert macos.apply_backdrop(w, "vibrancy") is True
    by_target = [(t, a) for t, a, _ in fake_appkit.log]
    assert ("effect", "setMaterial_") in by_target and ("effect", "setBlendingMode_") in by_target
    add = next(args for t, a, args in fake_appkit.log if t == "frame")
    assert add[1] == macos._BELOW and add[2] is view  # beneath the Qt view, relative to it
    assert ("window", "setOpaque_", (False,)) in fake_appkit.log
    assert ("window", "setBackgroundColor_", ("clear",)) in fake_appkit.log


@pytest.mark.parametrize("kwargs", [{"has_window": False}, {"has_frame": False}])
def test_vibrancy_fails_cleanly_when_view_is_not_attached(fake_appkit, monkeypatch, qapp, kwargs):
    install_objc(monkeypatch, FakeView(fake_appkit.log, **kwargs))
    assert macos.apply_backdrop(QMainWindow(), "vibrancy") is False


def test_vibrancy_failure_inside_appkit_returns_false(fake_appkit, monkeypatch, qapp):
    objc = ModuleType("objc")
    objc.objc_object = lambda c_void_p: (_ for _ in ()).throw(RuntimeError("bad pointer"))
    monkeypatch.setitem(sys.modules, "objc", objc)
    assert macos.apply_backdrop(QMainWindow(), "vibrancy") is False
    assert macos.apply_backdrop(QMainWindow(), "mica") is False


def test_facade_dispatches_to_macos(fake_appkit, monkeypatch, qapp):
    monkeypatch.setattr(sys, "platform", "darwin")
    install_objc(monkeypatch, FakeView(fake_appkit.log))
    assert native.apply_backdrop(QMainWindow(), "vibrancy") is True


@pytest.mark.parametrize("requested, expected", [("dark", "dark-aqua"), ("light", "aqua")])
def test_appearance_falls_back_to_nsapp_on_old_qt(fake_appkit, monkeypatch, requested, expected):
    from PySide6.QtGui import QGuiApplication
    monkeypatch.setattr(QGuiApplication, "styleHints", staticmethod(lambda: SimpleNamespace()))  # Qt < 6.8
    macos.set_appearance(requested)
    assert ("app", "setAppearance_", (f"appearance:{expected}",)) in fake_appkit.log


def test_system_appearance_clears_the_override(fake_appkit, monkeypatch):
    from PySide6.QtGui import QGuiApplication
    monkeypatch.setattr(QGuiApplication, "styleHints", staticmethod(lambda: SimpleNamespace()))
    macos.set_appearance("system")
    assert ("app", "setAppearance_", (None,)) in fake_appkit.log


def test_process_name_replaces_python(fake_appkit, monkeypatch):
    info = {"CFBundleName": "Python"}
    foundation = ModuleType("Foundation")
    foundation.NSBundle = SimpleNamespace(mainBundle=lambda: SimpleNamespace(
        localizedInfoDictionary=lambda: None, infoDictionary=lambda: info))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    macos.set_process_name()
    assert info["CFBundleName"] == macos.APP_NAME


def test_process_name_keeps_a_real_bundle_name(fake_appkit, monkeypatch):
    info = {"CFBundleName": "Subreddit Sentiment"}
    foundation = ModuleType("Foundation")
    foundation.NSBundle = SimpleNamespace(mainBundle=lambda: SimpleNamespace(
        localizedInfoDictionary=lambda: None, infoDictionary=lambda: info))
    monkeypatch.setitem(sys.modules, "Foundation", foundation)
    macos.set_process_name()
    assert info == {"CFBundleName": "Subreddit Sentiment"}


def test_prepare_window_only_goes_full_size_for_vibrancy(monkeypatch, qapp):
    monkeypatch.setattr(macos, "IS_MACOS", True)
    from PySide6.QtCore import Qt
    hint = getattr(Qt.WindowType, "ExpandedClientAreaHint", None)
    plain, full = QMainWindow(), QMainWindow()
    macos.prepare_window(plain, full_size=False)
    macos.prepare_window(full, full_size=True)
    if hint is not None:
        assert not plain.windowFlags() & hint
        assert full.windowFlags() & hint
