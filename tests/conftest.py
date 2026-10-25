# The clock is pinned before anything reads it (see fake_archive.Clock).
from fake_archive import Clock
Clock.pin()

# Imported first, so every test's `import sqlite3` gets the SQLite the app uses (see subsentiment/__init__.py).
import subsentiment  # noqa: F401

import gc
import sys

import re

import pytest

# CI runs the suite as jobs named after what they cover (see .github/workflows/tests.yml), each picking its areas
# with --area. "shared" is the files that touch no Qt, SQLite, files, processes or OS calls and have never failed
# on one OS only, so CI runs them once, on Linux. Every other area runs on each OS: Qt rendering and timing, the
# SQLite Linux swaps in, paths and processes have all failed on just one of them.
SHARED = {"test_arctic_shift.py", "test_lint.py", "test_reddit.py", "test_sentiment.py"}
AREAS = {
    "shared": "the archive client, Reddit helpers and sentiment scoring",
    "layout-small": "the layout matrix on screens up to 1920 wide",
    "layout-large": "the layout matrix on 1440p, 1600p and 4K screens",
    "coverage": "the Coverage page, driven in the app",
    "app": "the rest of the app driven end to end: fetching, switching, closing",
    "units": "the window's parts, storage, the fetch engine, packaging and platform code, one at a time",
}


def area(file: str, test: str) -> str:
    if file in SHARED:
        return "shared"
    if file == "test_layout.py":
        return "layout-large" if re.search(r"-(2560|2880|3840)x", test) else "layout-small"
    if file == "test_coverage_e2e.py" or (file == "test_app_e2e.py" and "coverage" in test):
        return "coverage"
    if file in ("test_app_e2e.py", "test_fetch_end_to_end.py"):
        return "app"
    return "units"


def pytest_addoption(parser):
    parser.addoption("--area", metavar="A,B", help="only these areas: " + "; ".join(
        f"{name} ({about})" for name, about in AREAS.items()))
    parser.addoption("--shard", metavar="K/N", help="only every Nth of those tests, starting at the Kth (1-based)")


def pytest_ignore_collect(collection_path, config):
    # The shared files are left out, or kept alone, before anything is imported, so the shared job runs where
    # Qt's libraries aren't installed.
    areas, name = config.getoption("--area"), collection_path.name
    if areas and name.startswith("test_") and name.endswith(".py"):
        shared = set(areas.split(",")) == {"shared"}
        return (name in SHARED) != shared or None


def pytest_collection_modifyitems(config, items):
    areas, shard = config.getoption("--area"), config.getoption("--shard")
    keep = items
    if areas:
        wanted = set(areas.split(","))
        if wanted - AREAS.keys():
            raise pytest.UsageError(f"unknown --area {', '.join(sorted(wanted - AREAS.keys()))}; known: {', '.join(AREAS)}")
        keep = [item for item in keep if area(item.path.name, item.name) in wanted]
    if shard:
        # Every Nth test rather than every Nth file, so long files spread evenly.
        k, n = map(int, shard.split("/"))
        keep = keep[k - 1::n]
    if keep is not items:
        kept = set(map(id, keep))
        config.hook.pytest_deselected(items=[item for item in items if id(item) not in kept])
        items[:] = keep


# Qt's warnings (a signal connected to nothing, a timer or child made on the wrong thread, a painter on a widget not
# being painted) fail the test they came during. The offscreen platform the tests draw on can't move, raise or
# resize windows the way a desktop does, and says so; those are left out.
QT_WARNINGS: list[str] = []
OFFSCREEN = "This plugin does not support"


def qt_message(mode, context, message):
    from PySide6.QtCore import QtMsgType
    print(message, file=sys.stderr)
    if mode != QtMsgType.QtDebugMsg and mode != QtMsgType.QtInfoMsg and not message.startswith(OFFSCREEN):
        QT_WARNINGS.append(message)


@pytest.fixture(autouse=True)
def no_qt_warnings():
    if "PySide6.QtCore" not in sys.modules:  # (the shared job has no Qt)
        yield
        return
    from PySide6.QtCore import qInstallMessageHandler
    qInstallMessageHandler(qt_message)
    QT_WARNINGS.clear()
    yield
    found, QT_WARNINGS[:] = list(QT_WARNINGS), []
    assert not found, "Qt warned:\n" + "\n".join(found)


def pytest_sessionfinish(session, exitstatus):
    """Delete the windows and tables tests left open while Python is whole: left to Qt's clean-up on the way out,
    their numpy arrays were freed after numpy itself, and the process aborted."""
    if "PySide6.QtWidgets" not in sys.modules:  # no test opened Qt (the shared job needs no Qt libraries)
        return
    from PySide6.QtWidgets import QApplication
    import shiboken6
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
    gc.collect()
