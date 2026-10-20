"""Fades for new data and for view changes.

Everything here changes only how a widget gets to its new state, never the state itself: the new contents are
in place at once (and `text()` already returns the final value), the old look just fades away over them. With
motion off (View > Reduce motion, or Windows' Animation effects turned off) each helper does nothing.

- `crossfade(widget)`: call just before changing what a widget shows. A still picture of how it looks now
  fades out over the new contents.
- `fade_in(widget)`: a widget that has just appeared fades in.
- `Tween`: eases a number from its old value to a new one, for counts and bar heights.

Fading a still picture, rather than the live widgets, means charts and tables aren't repainted on every frame
and no widget moves, so layouts stay exactly as they are.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QRect, Qt, QTimer, QVariantAnimation
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QWidget

from subsentiment.gui import native

VIEW_MS = 220   # a page, tab or filter changing
THEME_MS = 320  # light to dark and back
DATA_MS = 420   # numbers and bars easing to new values
APPEAR_MS = 200  # a status line or window appearing
STAGGER_MS = 45  # between cards fading in one after another

_wanted = True  # the View > Reduce motion setting, inverted
_os_allows: bool | None = None


def set_enabled(on: bool) -> None:
    """Turn motion on or off for the whole app (View > Reduce motion), including Qt's menu and list effects."""
    global _wanted
    _wanted = on
    effects = enabled()
    for effect in (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo,
                   Qt.UIEffect.UI_AnimateTooltip, Qt.UIEffect.UI_FadeTooltip):
        QApplication.setEffectEnabled(effect, effects)


def enabled() -> bool:
    global _os_allows
    if _os_allows is None:
        _os_allows = native.animations_on()
    return _wanted and _os_allows


def _ease(anim: QVariantAnimation) -> QVariantAnimation:
    anim.setEasingCurve(QEasingCurve.Type.OutCubic)
    return anim


# -- crossfade ----------------------------------------------------------------------------------------------

class _Snapshot(QLabel):
    """A still of a widget's old look, laid over it and fading out. Gone at once if the widget is resized,
    so a stale picture never sits over a new layout."""

    def __init__(self, target: QWidget, ms: int):
        super().__init__(target)
        self.setObjectName("motion-snapshot")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        window = target.window()
        # Grabbed from the window, so the backdrop behind see-through panels is in the picture too and the
        # fade is a true crossfade rather than two layers of glass.
        origin = target.mapTo(window, QPoint(0, 0))
        self.setPixmap(window.grab(QRect(origin, target.size())))
        self.setGeometry(target.rect())
        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self.anim = _ease(QVariantAnimation(self, duration=ms, startValue=1.0, endValue=0.0))
        self.anim.valueChanged.connect(self._effect.setOpacity)
        self.anim.finished.connect(self.deleteLater)
        target.installEventFilter(self)
        self.show()
        self.raise_()
        self.anim.start()

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            obj.removeEventFilter(self)
            self.anim.stop()
            self.deleteLater()
        return False


def crossfade(target: QWidget, ms: int = VIEW_MS) -> None:
    """Call just before changing what target shows: its current look fades out over the new one."""
    if not enabled() or not target.isVisible() or target.width() <= 0 or target.height() <= 0:
        return
    parent = target.parentWidget()
    while parent is not None:
        if fading(parent):  # a fade over a whole area already covers this part of it: don't grab again
            return
        parent = parent.parentWidget()
    for old in target.findChildren(_Snapshot, options=Qt.FindChildOption.FindDirectChildrenOnly):
        # A fade already running: start from what's on screen now, not from the picture before it.
        old.hide()
        old.deleteLater()
    _Snapshot(target, ms)


def fading(target: QWidget) -> bool:
    """Whether a crossfade is running over target (for tests)."""
    return any(s.isVisible() for s in target.findChildren(_Snapshot, options=Qt.FindChildOption.FindDirectChildrenOnly))


# -- fade in ------------------------------------------------------------------------------------------------

def fade_in(widget: QWidget, ms: int = APPEAR_MS, delay: int = 0) -> None:
    """Fade a widget in from transparent. Its opacity effect is removed when done, so it paints as before."""
    if not enabled() or not widget.isVisible():
        return
    if isinstance(widget.graphicsEffect(), QGraphicsOpacityEffect) and widget.property("motion_fading"):
        return  # already fading in
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)
    widget.setProperty("motion_fading", True)
    anim = _ease(QVariantAnimation(effect, duration=ms, startValue=0.0, endValue=1.0))
    anim.valueChanged.connect(effect.setOpacity)

    def done():
        widget.setProperty("motion_fading", False)
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)
    anim.finished.connect(done)
    if delay:
        QTimer.singleShot(delay, anim, anim.start)  # cancelled if the widget (and its anim) goes first
    else:
        anim.start()


def fade_in_all(widgets: list[QWidget], ms: int = DATA_MS) -> None:
    """Fade widgets in one after another, a few frames apart."""
    for i, w in enumerate(widgets):
        fade_in(w, ms, delay=i * STAGGER_MS)


def fade_window(window: QWidget, ms: int = APPEAR_MS) -> None:
    """A window that has just opened fades in (where the platform supports window opacity)."""
    if not enabled():
        return
    window.setWindowOpacity(0.0)
    anim = _ease(QVariantAnimation(window, duration=ms, startValue=0.0, endValue=1.0))
    anim.valueChanged.connect(window.setWindowOpacity)
    anim.finished.connect(lambda: window.setWindowOpacity(1.0))
    anim.start()


# -- tween --------------------------------------------------------------------------------------------------

class Tween(QObject):
    """Eases a float to each new target, calling apply with every in-between value and finally the target.

    A new target while one is running continues from the value on screen."""

    def __init__(self, apply: Callable[[float], None], parent: QObject | None = None, ms: int = DATA_MS):
        super().__init__(parent)
        self._apply = apply
        self.value: float | None = None  # what's on screen now
        self.target: float | None = None
        self._anim = _ease(QVariantAnimation(self, duration=ms))
        self._anim.valueChanged.connect(self._step)

    def set(self, target: float, animate: bool = True) -> None:
        self.target = target
        self._anim.stop()
        if not animate or not enabled() or self.value is None or self.value == target:
            self._step(target)
            return
        self._anim.setStartValue(float(self.value))
        self._anim.setEndValue(float(target))
        self._anim.start()

    def stop(self) -> None:
        self._anim.stop()

    def running(self) -> bool:
        return self._anim.state() == QVariantAnimation.State.Running

    def _step(self, v) -> None:
        self.value = float(v)
        self._apply(self.value)
