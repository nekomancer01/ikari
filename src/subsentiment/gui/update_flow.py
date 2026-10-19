"""Menu-driven "check for updates" flow: query GitHub, ask, download, hand off to the installer."""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QSettings, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog, QWidget

from subsentiment import __version__, updater


class UpdateFlow(QObject):
    _checked = Signal(object, bool)  # Release | UpdateError, silent
    _progress = Signal(int, int)
    _downloaded = Signal(object)  # Path | UpdateError

    def __init__(self, window: QWidget, settings: QSettings):
        super().__init__(window)
        self.window, self.settings = window, settings
        self._busy = False
        self._cancel = False
        self._dialog: QProgressDialog | None = None
        self._checked.connect(self._on_checked)
        self._progress.connect(self._on_progress)
        self._downloaded.connect(self._on_downloaded)

    def check(self, silent: bool = False) -> None:
        """silent=True is the startup check: say nothing unless a new version exists."""
        if self._busy:
            return
        self._busy = True

        def run():
            try:
                result = updater.latest_release()
            except updater.UpdateError as e:
                result = e
            self._checked.emit(result, silent)
        threading.Thread(target=run, daemon=True).start()

    def _on_checked(self, result, silent: bool) -> None:
        self._busy = False
        if isinstance(result, updater.UpdateError):
            if not silent:
                QMessageBox.warning(self.window, "Check for updates", str(result))
            return
        if not updater.is_newer(result.version):
            if not silent:
                QMessageBox.information(self.window, "Check for updates",
                                        f"You are up to date (version {__version__}).")
            return
        if silent and self.settings.value("updates/skip", "", str) == result.version:
            return
        self._offer(result, silent)

    def _offer(self, rel: updater.Release, silent: bool) -> None:
        box = QMessageBox(self.window)
        box.setWindowTitle("Update available")
        box.setText(f"Version {rel.version} is available (you have {__version__}).")
        if rel.notes.strip():
            box.setInformativeText(rel.notes.strip()[:600])
        install = None
        if rel.asset_url:
            install = box.addButton("Download and install", QMessageBox.ButtonRole.AcceptRole)
        else:
            install = box.addButton("Open release page", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        skip = box.addButton("Skip this version", QMessageBox.ButtonRole.DestructiveRole) if silent else None
        box.exec()
        clicked = box.clickedButton()
        if clicked is skip and skip is not None:
            self.settings.setValue("updates/skip", rel.version)
        elif clicked is install:
            if rel.asset_url:
                self._download(rel)
            else:
                QDesktopServices.openUrl(QUrl(rel.page_url))

    def _download(self, rel: updater.Release) -> None:
        self._busy, self._cancel = True, False
        dlg = QProgressDialog(f"Downloading {rel.asset_name}…", "Cancel", 0, 100, self.window)
        dlg.setWindowTitle("Updating")
        dlg.setMinimumDuration(0)
        dlg.canceled.connect(lambda: setattr(self, "_cancel", True))
        dlg.show()
        self._dialog = dlg
        dest = Path(tempfile.gettempdir()) / "subsentiment-update"

        def run():
            try:
                path = updater.download(rel, dest, progress=self._progress.emit,
                                        should_stop=lambda: self._cancel)
            except updater.UpdateError as e:
                path = e
            self._downloaded.emit(path)
        threading.Thread(target=run, daemon=True).start()

    def _on_progress(self, done: int, total: int) -> None:
        if self._dialog and total:
            self._dialog.setValue(int(done * 100 / total))

    def _on_downloaded(self, result) -> None:
        self._busy = False
        if self._dialog:
            self._dialog.close()
            self._dialog = None
        if isinstance(result, updater.UpdateError):
            if not self._cancel:
                QMessageBox.warning(self.window, "Update failed", str(result))
            return
        try:
            handed_off = updater.launch_installer(result)
        except updater.UpdateError as e:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.parent)))
            QMessageBox.warning(self.window, "Update failed",
                                f"{e}\n\nDownloaded {result.name}. Extract it over this copy to update.")
            return
        if handed_off:
            QApplication.quit()
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(result.parent)))
            QMessageBox.information(self.window, "Update downloaded",
                                    f"Downloaded {result.name}. Open it to finish updating.")
