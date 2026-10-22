# The clock is pinned before anything reads it (see fake_archive.Clock).
from fake_archive import Clock
Clock.pin()

# Imported first, so every test's `import sqlite3` gets the SQLite the app uses (see subsentiment/__init__.py).
import subsentiment  # noqa: F401

import gc


def pytest_sessionfinish(session, exitstatus):
    """Delete the windows and tables tests left open while Python is whole: left to Qt's clean-up on the way out,
    their numpy arrays were freed after numpy itself, and the process aborted."""
    from PySide6.QtWidgets import QApplication
    import shiboken6
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        if shiboken6.isValid(widget):
            shiboken6.delete(widget)
    gc.collect()
