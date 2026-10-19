import sys

from subsentiment.gui import win_native


def test_colorref_is_bgr():
    assert win_native._colorref("#112233") == 0x332211


def test_noop_off_windows():
    if sys.platform == "win32":
        return
    assert not win_native.backdrop_supported()
    assert win_native.apply_backdrop(0, "acrylic") is False
    win_native.style_title_bar(0, dark=True, caption="#000000", text="#ffffff")  # must not raise
    win_native.set_app_user_model_id()
