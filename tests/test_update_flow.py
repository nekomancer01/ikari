"""The Check for updates flow says what went wrong, and stays usable after anything fails."""
import os
import sys
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


def offer(f, monkeypatch, rel=REL):
    """Show the Update available box for rel, click its first button, and return the buttons' labels."""
    seen = []

    def click_first(box):
        seen.extend(b.text() for b in box.buttons())
        box.buttons()[0].click()
    monkeypatch.setattr(QMessageBox, "exec", click_first)
    f._offer(rel, silent=True)
    return seen


def test_run_from_source_an_update_opens_the_release_page_and_installs_nothing(flow, monkeypatch):
    # From a source checkout, "Download and install" ran ikari-Setup.exe, which installed a separate
    # packaged copy, and quit the session the user was running.
    f, app = flow
    monkeypatch.delattr(sys, "frozen", raising=False)
    opened, downloads = [], []
    monkeypatch.setattr(update_flow.QDesktopServices, "openUrl", lambda url: opened.append(url.toString()))
    monkeypatch.setattr(f, "_download", downloads.append)
    assert "Download and install" not in offer(f, monkeypatch)
    assert opened == ["https://x"] and downloads == []


def test_a_packaged_copy_still_downloads_and_installs(flow, monkeypatch):
    f, app = flow
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    downloads = []
    monkeypatch.setattr(f, "_download", downloads.append)
    assert offer(f, monkeypatch)[0] == "Download and install"
    assert downloads == [REL]


def test_an_installer_that_wont_start_is_reported(flow, monkeypatch, tmp_path):
    f, app = flow
    setup = tmp_path / "ikari-Setup-9.9.9.exe"

    def blocked(path):
        raise updater.UpdateError("Could not start the installer: [WinError 225] virus detected")
    monkeypatch.setattr(updater, "launch_installer", blocked)
    monkeypatch.setattr(update_flow.QDesktopServices, "openUrl", lambda url: None)
    f._on_downloaded(setup)
    assert f.messages == [("warning", "Update failed", "Could not start the installer: [WinError 225] virus "
                           "detected\n\nDownloaded ikari-Setup-9.9.9.exe. Open it to finish updating.")]
