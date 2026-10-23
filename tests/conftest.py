# The clock is pinned before anything reads it (see fake_archive.Clock).
from fake_archive import Clock
Clock.pin()

# Imported first, so every test's `import sqlite3` gets the SQLite the app uses (see subsentiment/__init__.py).
import subsentiment  # noqa: F401

import gc
import sys

# CI splits the suite into parts (see .github/workflows/tests.yml). These files touch no Qt, SQLite, files,
# processes or OS calls and have never failed on one OS only, so CI runs them once, on Linux, as "shared".
# Every other file runs on each OS: Qt rendering and timing, the SQLite Linux swaps in, paths and processes
# have all failed on just one of them.
SHARED = {"test_arctic_shift.py", "test_reddit.py", "test_sentiment.py"}


def pytest_addoption(parser):
    parser.addoption("--part", choices=["shared", "layout", "other"],
                     help="only the shared tests, the layout matrix, or the rest (CI's split; default: all)")
    parser.addoption("--shard", metavar="K/N", help="only every Nth test, starting at the Kth (1-based)")


def pytest_ignore_collect(collection_path, config):
    # Left out before they're imported, so the shared part runs where Qt's libraries aren't installed.
    part, name = config.getoption("--part"), collection_path.name
    if part and name.startswith("test_") and name.endswith(".py"):
        kind = "shared" if name in SHARED else "layout" if name == "test_layout.py" else "other"
        return kind != part or None


def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--shard")
    if not shard:
        return
    # Every Nth test rather than every Nth file, so the long e2e file and the layout rows spread evenly.
    k, n = map(int, shard.split("/"))
    keep = items[k - 1::n]
    kept = set(map(id, keep))
    config.hook.pytest_deselected(items=[item for item in items if id(item) not in kept])
    items[:] = keep


def pytest_sessionfinish(session, exitstatus):
    """Delete the windows and tables tests left open while Python is whole: left to Qt's clean-up on the way out,
    their numpy arrays were freed after numpy itself, and the process aborted."""
    if "PySide6.QtWidgets" not in sys.modules:  # no test opened Qt (the shared part needs no Qt libraries)
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
