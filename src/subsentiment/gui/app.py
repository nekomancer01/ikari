from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QApplication

from subsentiment.gui import native, theme
from subsentiment.gui.main_window import MainWindow


# Storage identifiers predate the rename to "ikari". They stay as they were so the existing database
# and QSettings keep working: the app-data folder and settings file are keyed on these two names.
STORAGE_ORG = "subsentiment"
STORAGE_APP = "Subreddit Sentiment"


def default_db_path() -> str:
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    return str(base / "subsentiment.db")


def main(argv: list[str] | None = None) -> int:
    native.setup_app_identity()  # before the app and any window, so taskbar/dock/menu use our name and icon
    app = QApplication(theme.qt_args(list(argv if argv is not None else sys.argv)))
    app.setOrganizationName(STORAGE_ORG)
    app.setApplicationName(STORAGE_APP)  # drives storage paths only; the visible name is the display name
    app.setApplicationDisplayName("ikari")
    settings = QSettings()
    settings.remove("reddit")  # Reddit API credentials from versions that supported the official API
    theme.load_fonts(app)
    requested = settings.value("ui/theme", "system", str)
    native.set_appearance(requested)
    theme.apply(app, requested,
                settings.value("ui/reduce_transparency", False, bool))
    window = MainWindow(default_db_path())
    window.show()
    return app.exec()
