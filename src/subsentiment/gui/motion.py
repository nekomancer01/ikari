"""Fades for new data and for view changes.

Everything here changes only how a widget gets to its new state, never the state itself: the new contents are
in place at once (and `text()` already returns the final value), the old look just fades away over them. With
motion off (View > Reduce motion, or Windows' Animation effects turned off) each helper does nothing.

- `crossfade(widget)`: call just before changing what a widget shows. A still picture of how it looks now
  fades out over the new contents.
- `fade_in(widget)`: a widget that has just appeared fades in.
- `Tween`: eases a number from its old value to a new one, for counts and bar heights.
- `throb(widget)`: where a waiting widget's throbber is, for a widget that paints one while it waits.

Fading a still picture, rather than the live widgets, means charts and tables aren't repainted on every frame
and no widget moves, so layouts stay exactly as they are. The new look under it is held as a picture too, and
grabbed again only while something there moves (a count easing, a throbber) or changes view.
"""
from __future__ import annotations

import math
import time
from typing import Callable

import shiboken6
from PySide6.QtCore import QEasingCurve, QEvent, QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QGraphicsOpacityEffect, QLabel, QWidget

from subsentiment.gui import native

VIEW_MS = 220   # a page, tab or filter changing
THEME_MS = 320  # light to dark and back
DATA_MS = 420   # numbers and bars easing to new values
APPEAR_MS = 200  # a status line or window appearing
THROB_MS = 1400  # one sweep of a throbber
REGRAB_MS = 80  # how stale the new look under a fade's still may get

_wanted = True  # the View > Reduce motion setting, inverted
_os_allows: bool | None = None
_EFFECTS = (Qt.UIEffect.UI_AnimateMenu, Qt.UIEffect.UI_FadeMenu, Qt.UIEffect.UI_AnimateCombo,
            Qt.UIEffect.UI_AnimateTooltip, Qt.UIEffect.UI_FadeTooltip)
_os_effects: dict | None = None  # which of Qt's effects the OS had on, read before we first change them


def set_enabled(on: bool) -> None:
    """Turn motion on or off for the whole app (View > Reduce motion), including Qt's menu and list effects."""
    global _wanted, _os_effects
    _wanted = on
    if _os_effects is None:
        _os_effects = {e: QApplication.isEffectEnabled(e) for e in _EFFECTS}
    for effect, os_on in _os_effects.items():  # never on where the OS has it off ("Fade or slide menus")
        QApplication.setEffectEnabled(effect, os_on and enabled())


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
    so a stale picture never sits over a new layout.

    The new look is held as a picture too and the fade blends the two: the still is opaque, so nothing under
    it repaints on every frame (a whole page takes 10-25 ms to paint at 150%, more than a frame with the
    fade's own drawing). The new look is grabbed whole every ``regrab_ms`` (REGRAB_MS for a view change),
    so whatever changes under the fade shows within a few frames, and on every frame the parts that are animating or changing view are grabbed
    again, so a count easing or a page switching mid-fade moves as smoothly as without it.

    The still lines up with the window's own pixels. At 150% a widget at x=105 starts at device pixel 157.5,
    and a grab from there is rounded to 157 or 158: the old look then sat a pixel off and everything under a
    fade jumped sideways and back. So the grab starts a little up and left, at a whole device pixel, and is
    drawn back at that same pixel."""

    def __init__(self, target: QWidget, ms: int, regrab_ms: int | None):
        super().__init__(target)
        self.setObjectName("motion-snapshot")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        window = target.window()
        # Grabbed from the window, so the backdrop behind see-through panels is in the picture too and the
        # fade is a true crossfade rather than two layers of glass.
        origin = target.mapTo(window, QPoint(0, 0))
        self._lead = lead(target)
        self._still = _picture(window, QRect(origin - self._lead, target.size() + QSize(self._lead.x(), self._lead.y())))
        self.opacity = 1.0
        self._under: QPixmap | None = None  # the new look, grabbed on the first frame
        self._grabbed = 0.0  # when it was last grabbed whole
        self._regrab = math.inf if regrab_ms is None else regrab_ms / 1000
        self.changed: list[QWidget] = []  # parts under it whose view changed, to grab again on the next frame
        self._grabbing = False
        self.setGeometry(target.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)  # the still covers all of it until the first frame
        self.anim = _ease(QVariantAnimation(self, duration=ms, startValue=1.0, endValue=0.0))
        self.anim.valueChanged.connect(self._fade)
        self.anim.finished.connect(self.deleteLater)
        target.installEventFilter(self)
        self.show()
        self.raise_()
        self.anim.start()

    def _fade(self, value: float) -> None:
        self.opacity = value
        whole = self._under is None or time.monotonic() - self._grabbed >= self._regrab
        parts = [] if whole else [w for w in self.changed + self._moving() if shiboken6.isValid(w) and w.isVisible()]
        self.changed = []
        if whole or parts:
            window, target = self.window(), self.parentWidget()
            origin = target.mapTo(window, QPoint(0, 0)) - self._lead
            # Grabbed with this still left out of the picture (hiding it would repaint all under it again), and
            # any fade over it too (one started around this one, late in its own fade); fades inside it stay in.
            left_out = [s for s in window.findChildren(_Snapshot) if s is self or not target.isAncestorOf(s)]
            for s in left_out:
                s._grabbing = True
                s.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
            if whole:
                self._under = window.grab(QRect(origin, self._still.deviceIndependentSize().toSize()))
                self._grabbed = time.monotonic()
            else:
                p = QPainter(self._under)
                for w in parts:  # a little past the part's edges, which fall between device pixels at 150%
                    piece = QRect(w.mapTo(window, QPoint(0, 0)), w.size()).adjusted(-2, -2, 2, 2)
                    window.render(p, piece.topLeft() - origin, QRegion(piece))
                p.end()
            for s in left_out:
                s.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
                s._grabbing = False
        self.update()

    def _moving(self) -> list[QWidget]:
        """The widgets under the still that are animating: a count or bar easing (Tween), a widget fading in,
        another fade, a throbber."""
        target, moving = self.parentWidget(), []
        for a in target.findChildren(QVariantAnimation):
            if a is not self.anim and a.state() == QVariantAnimation.State.Running:
                owner = a.parent()
                while owner is not None and not isinstance(owner, QWidget):
                    owner = owner.parent()
                if owner is not None:
                    moving.append(owner)
        if _throbbers is not None:
            moving += [w for w in _throbbers.moving() if shiboken6.isValid(w) and target.isAncestorOf(w)]
        return moving

    def paintEvent(self, event) -> None:
        if self._grabbing:
            return
        p = QPainter(self)
        if self._under is not None:
            p.drawPixmap(-self._lead, self._under)
        p.setOpacity(self.opacity)
        p.drawPixmap(-self._lead, self._still)

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Resize:
            obj.removeEventFilter(self)
            self.anim.stop()
            self.deleteLater()
        return False


def _picture(window: QWidget, rect: QRect) -> QPixmap:
    """The window's pixels in rect as they were last painted. Where the window's backing store is a plain image
    (Windows, X11, offscreen) they are copied from it, which takes a few milliseconds; rendering the window again
    (grab) takes about 40 at 150%. Elsewhere (Wayland swaps buffers, so the image may hold an older frame) and
    whenever the image doesn't match the window, it's rendered."""
    store = window.backingStore() if QGuiApplication.platformName() in ("windows", "xcb", "offscreen") else None
    image = store.paintDevice() if store is not None else None
    dpr = window.devicePixelRatioF()
    r = QRectF(rect)
    device = QRectF(r.topLeft() * dpr, r.size() * dpr).toAlignedRect()
    if (isinstance(image, QImage) and abs(image.width() - window.width() * dpr) < 1
            and abs(image.height() - window.height() * dpr) < 1 and image.rect().contains(device)):
        picture = QPixmap.fromImage(image.copy(device))
        picture.setDevicePixelRatio(dpr)
        return picture
    return window.grab(rect)


def lead(widget: QWidget) -> QPoint:
    """How far up and left of widget's corner the nearest point on a whole device pixel of its window is. A
    picture of a widget painted from there and drawn back there lines up with what painting it directly gives;
    one drawn at the corner itself is a pixel off at 150% wherever the corner falls between device pixels."""
    origin, dpr = widget.mapTo(widget.window(), QPoint(0, 0)), widget.window().devicePixelRatioF()
    return QPoint(_to_whole_pixel(origin.x(), dpr), _to_whole_pixel(origin.y(), dpr))


def _to_whole_pixel(at: int, dpr: float) -> int:
    """How far back from logical coordinate ``at`` the nearest one on a whole device pixel is (0 at 100% and
    200%, 0 or 1 at 150%, up to 3 at 125%)."""
    return next((k for k in range(8) if abs((at - k) * dpr - round((at - k) * dpr)) < 1e-6), 0)


def crossfade(target: QWidget, ms: int = VIEW_MS, regrab_ms: int | None = REGRAB_MS) -> None:
    """Call just before changing what target shows: its current look fades out over the new one. The new look
    is grabbed whole again every ``regrab_ms`` during the fade; with None only once (the theme, which repaints
    every widget and changes nothing more while it fades)."""
    if not enabled() or not target.isVisible() or target.width() <= 0 or target.height() <= 0:
        return
    parent = target.parentWidget()
    while parent is not None:
        if fading(parent, early=True):  # a fade over a whole area already covers this part: don't grab again,
            for over in _snapshots(parent):  # but show the change in its new look
                over.changed.append(target)
            return
        parent = parent.parentWidget()
    if target.window().testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground):
        # Behind a native backdrop (Mica, Acrylic) the system draws the glass, so a still has see-through
        # panels and would lay glass over glass. The new look fades in over the backdrop instead (not a
        # whole window: an effect on the top-level widget would hide the system's glass behind it).
        if not target.isWindow():
            fade_in(target, ms)
        return
    # A fade already running: start from what's on screen now, not from the picture before it. The new look is
    # held as a picture, which would hold any fade inside target at its current frame too, so those go as well.
    for old in target.findChildren(_Snapshot):
        old.anim.stop()
        old.hide()
        old.deleteLater()
    _Snapshot(target, ms, regrab_ms)


def fading(target: QWidget, early: bool = False) -> bool:
    """Whether a crossfade is running over target; with early, only one in its first half, whose picture
    still mostly hides what changes under it."""
    return any(not early or s.anim.currentTime() < s.anim.duration() / 2 for s in _snapshots(target))


def _snapshots(target: QWidget) -> list[_Snapshot]:
    return [s for s in target.findChildren(_Snapshot, options=Qt.FindChildOption.FindDirectChildrenOnly)
            if s.isVisible()]


# -- fade in ------------------------------------------------------------------------------------------------

def fade_in(widget: QWidget, ms: int = APPEAR_MS) -> None:
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


# -- throbber -----------------------------------------------------------------------------------------------

class _Throbbers(QObject):
    """One clock for every throbber on screen, so twenty of them cost one timer and stay in step. A widget
    asks for the phase as it paints and is repainted on the next tick; one that stops painting a throbber (it
    got its number, or was hidden or scrolled away) simply stops asking, and with none left the clock stops."""

    def __init__(self):
        super().__init__()
        self._painted: list[QWidget] = []
        self._ticked: list[QWidget] = []  # the ones repainted on the last tick
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)

    def phase(self, widget: QWidget) -> float | None:
        if not enabled():
            return None
        if widget not in self._painted:
            self._painted.append(widget)
        if not self._timer.isActive():
            self._timer.start()
        return time.monotonic() * 1000 % THROB_MS / THROB_MS

    def moving(self) -> list[QWidget]:
        return self._painted + self._ticked

    def _tick(self) -> None:
        painted, self._painted = self._painted, []
        self._ticked = painted
        for w in painted:
            if shiboken6.isValid(w):  # a table row rebuilt since deletes its widgets
                w.update()
        if not painted:
            self._timer.stop()


_throbbers: _Throbbers | None = None


def throb(widget: QWidget) -> float | None:
    """For a widget painting a throbber: how far through its sweep it is (0 to 1), and widget is repainted
    on the next frame. None with motion off, for a still "waiting" look instead."""
    global _throbbers
    if _throbbers is None:
        _throbbers = _Throbbers()
    return _throbbers.phase(widget)
