"""Panel, table, input and button outlines at 150% scaling: one crisp device pixel, never a solid pixel with a half-toned
one beside it (a 1 px style sheet border is 1.5 device pixels there, which looked fuzzy)."""
import os

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QRectF  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QComboBox, QLineEdit, QPushButton, QTableView, QWidget  # noqa: E402

from subsentiment.gui import theme, widgets  # noqa: E402


def rendered(make, at: QPoint, mode: str) -> tuple[QImage, QWidget, QWidget]:
    """``make()`` placed at ``at`` (logical) on a page of panel color, drawn at a 1.5 device pixel ratio."""
    app = QApplication.instance() or QApplication([])
    page = QWidget()
    theme.apply(app, mode, window=page)
    page.setStyleSheet(page.styleSheet() + f"QWidget#page {{ background: {theme.tokens()['panel']}; }}")
    page.setObjectName("page")
    page.resize(300, 200)
    child = make()
    child.setParent(page)
    child.setGeometry(at.x(), at.y(), 160, 90 if not isinstance(child, (QLineEdit, QComboBox, QPushButton)) else 32)
    page.ensurePolished()
    image = QImage(450, 300, QImage.Format.Format_ARGB32)
    image.setDevicePixelRatio(1.5)
    image.fill(QColor(theme.tokens()["panel"]))
    page.render(image)
    return image, child, page  # the page owns the child: keep it alive


@pytest.mark.parametrize("mode", ["light", "dark"])
@pytest.mark.parametrize("at", [QPoint(20, 20), QPoint(21, 21)])  # an odd offset lands on half a device pixel
@pytest.mark.parametrize("make", [lambda: widgets.panel("tile")[0], lambda: widgets.panel("true")[0],
                                  QTableView, QLineEdit, lambda: theme.mark(QComboBox(), "pill", True),
                                  lambda: QPushButton("Go"), lambda: theme.mark(QPushButton("Go"), "pill", "compact")],
                         ids=["tile", "panel", "table", "input", "pill", "button", "pill-button"])
def test_edges_are_one_crisp_device_pixel(make, at, mode):
    image, child, _page = rendered(make, at, mode)
    panel = QColor(theme.tokens()["panel"]).rgb()
    mid_y = round((at.y() + child.height() / 2) * 1.5)
    mid_x = round((at.x() + child.width() / 2) * 1.5)
    # Across the left and the top edge, from outside the widget into it.
    left = [image.pixel(x, mid_y) for x in range(round(at.x() * 1.5) - 3, round(at.x() * 1.5) + 4)]
    top = [image.pixel(mid_x, y) for y in range(round(at.y() * 1.5) - 3, round(at.y() * 1.5) + 4)]
    for line in (left, top):
        inked = [c for c in line if c != panel]
        assert len(inked) == 1, [hex(c) for c in line]  # one pixel of edge, nothing half-toned beside it


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 2.0])
def test_cut_corners_give_the_pixels_of_drawing_the_whole_box(dpr):
    # Outline.draw copies corners it drew once and fills the rest with plain rectangles (a big anti-aliased
    # rounded rect was the slowest part of painting a window); that must look like drawing the box whole.
    QApplication.instance() or QApplication([])
    edge, fills = QColor(0, 0, 0, 24), (None, QColor("#ffffff"), QColor(28, 28, 30))

    def draw(whole: bool, rect: QRectF, radius: float, fill) -> QImage:
        image = QImage(round(420 * dpr), round(300 * dpr), QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(QColor("#f1f1f1"))
        p = QPainter(image)
        if whole:
            p.setOpacity(0.999999)  # not full opacity: drawn whole
        theme.Outline.draw(p, rect, radius, edge, fill)
        p.end()
        return image

    for rect, radius in ((QRectF(20, 20, 380, 250), 16), (QRectF(21, 21.5, 200.5, 90), 10),
                         (QRectF(7, 3, 120, 30), 15), (QRectF(30, 40, 90, 26), 6)):
        for fill in fills:
            images = [draw(w, rect, radius, fill) for w in (False, True)]
            cut, whole = (np.frombuffer(i.constBits(), np.uint8).astype(int) for i in images)
            assert np.abs(cut - whole).max() <= 3, (rect, radius, fill)  # rounding where premultiplied colours meet, not a visible change
