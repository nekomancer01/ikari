"""The Check for updates flow says what went wrong, and stays usable after anything fails."""
import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox, QPushButton  # noqa: E402

from subsentiment import updater  # noqa: E402
from subsentiment.gui import update_flow  # noqa: E402

REL = updater.Release("9.9.9", "v9.9.9", "", "https://x", "ikari-Setup-9.9.9.exe", "https://x/a.exe", 10)


@pytest.fixture
def flow(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path / "cfg"))
    window = QMainWindow()
    f = update_flow.UpdateFlow(window, QSettings())
    f.messages = []
    for kind in ("warning", "information"):
        monkeypatch.setattr(QMessageBox, kind, lambda *a, k=kind, **kw: f.messages.append((k, a[1], a[2])))
    yield f, app
    window.close()
    QSettings.setDefaultFormat(QSettings.Format.NativeFormat)


def settle(app, done, timeout=5.0):
    end = time.monotonic() + timeout
    while not done() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def test_a_failed_download_is_reported(flow, monkeypatch):
    # Closing the progress dialog emits canceled(), which used to mark the download as cancelled by the
    # user, so an HTTP error or a short download ended with no message at all.
    f, app = flow

    def fail(*a, **k):
        raise updater.UpdateError("Download failed: HTTP 500.")
    monkeypatch.setattr(updater, "download", fail)
    f._download(REL)
    settle(app, lambda: f.messages)
    assert f.messages == [("warning", "Update failed", "Download failed: HTTP 500.")]
    assert not f._busy


def test_a_download_the_user_cancels_ends_quietly(flow, monkeypatch):
    f, app = flow

    def slow(rel, dest, progress=None, should_stop=lambda: False, session=None):
        while not should_stop():
            time.sleep(0.01)
        raise updater.UpdateError("Download cancelled.")
    monkeypatch.setattr(updater, "download", slow)
    f._download(REL)
    f._dialog.findChild(QPushButton).click()  # the dialog's Cancel button
    settle(app, lambda: not f._busy)
    assert f.messages == [] and not f._busy


def test_an_unexpected_answer_from_github_is_reported_and_the_menu_still_works(flow, monkeypatch):
    # A captive portal answering 200 with a web page: r.json() raised outside any handler, the check
    # thread died, _busy stayed set and every later "Check for updates" did nothing.
    f, app = flow

    class Page:
        status_code, ok = 200, True

        def json(self):
            raise ValueError("Expecting value: line 1 column 1 (char 0)")

    class Session:
        def get(self, *a, **k):
            return Page()
    monkeypatch.setattr(updater, "latest_release",
                        lambda real=updater.latest_release: real(Session(), "win32"))
    f.check()
    settle(app, lambda: f.messages)
    assert f.messages and f.messages[0][:2] == ("warning", "Check for updates")
    assert "couldn't be read" in f.messages[0][2]
    f.messages.clear()
    f.check()  # still answers
    settle(app, lambda: f.messages)
    assert f.messages


def test_a_download_that_cant_be_saved_is_reported(flow, monkeypatch):
    # The antivirus holding the installer, a full disk: os.replace raised OSError, which nothing caught.
    f, app = flow

    def locked(*a, **k):
        raise PermissionError(13, "The process cannot access the file because it is being used by another process")
    monkeypatch.setattr(updater, "download", locked)
    f._download(REL)
    settle(app, lambda: f.messages)
    assert f.messages and f.messages[0][:2] == ("warning", "Update failed")
    assert "being used by another process" in f.messages[0][2]
    assert not f._busy
