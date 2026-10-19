from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QApplication

from subsentiment.gui import native, theme
from subsentiment.gui.main_window import MainWindow


def default_db_path() -> str:
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    return str(base / "subsentiment.db")


def main(argv: list[str] | None = None) -> int:
    native.setup_app_identity()  # before the app and any window, so taskbar/dock/menu use our name and icon
    app = QApplication(argv if argv is not None else sys.argv)
    app.setOrganizationName("subsentiment")
    app.setApplicationName("Subreddit Sentiment")
    settings = QSettings()
    theme.load_fonts(app)
    requested = settings.value("ui/theme", "system", str)
    native.set_appearance(requested)
    theme.apply(app, requested,
                settings.value("ui/reduce_transparency", False, bool))
    window = MainWindow(default_db_path())
    window.show()
    return app.exec()
