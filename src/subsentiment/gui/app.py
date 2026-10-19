from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths
from PySide6.QtWidgets import QApplication

from subsentiment.gui import theme, win_native
from subsentiment.gui.main_window import MainWindow


def default_db_path() -> str:
    base = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    return str(base / "subsentiment.db")


def main(argv: list[str] | None = None) -> int:
    win_native.set_app_user_model_id()  # before any window, so the taskbar uses our icon
    app = QApplication(argv if argv is not None else sys.argv)
    app.setOrganizationName("subsentiment")
    app.setApplicationName("Subreddit Sentiment")
    settings = QSettings()
    theme.load_fonts(app)
    theme.apply(app, settings.value("ui/theme", "system", str),
                settings.value("ui/reduce_transparency", False, bool))
    window = MainWindow(default_db_path())
    window.show()
    return app.exec()
