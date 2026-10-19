from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from subsentiment.reddit import DEFAULT_USER_AGENT


def load(settings: QSettings) -> dict:
    return {
        "client_id": settings.value("reddit/client_id", "", str),
        "client_secret": settings.value("reddit/client_secret", "", str),
        "user_agent": settings.value("reddit/user_agent", DEFAULT_USER_AGENT, str),
    }


class SettingsDialog(QDialog):
    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Reddit API settings")
        self.settings = settings
        values = load(settings)

        intro = QLabel(
            "Leave these empty to use Reddit's public endpoints (slow, and often blocked "
            "from VPN or cloud IPs).<br><br>For reliable access, create a <b>script</b> app at "
            "<a href='https://www.reddit.com/prefs/apps'>reddit.com/prefs/apps</a> and paste its "
            "client id (under the app name) and secret here. Only read access is used.<br><br>"
            "<i>The secret is stored unencrypted in this app's settings file.</i>")
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)

        self.client_id = QLineEdit(values["client_id"])
        self.client_secret = QLineEdit(values["client_secret"])
        self.client_secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.user_agent = QLineEdit(values["user_agent"])
        self.user_agent.setToolTip("Reddit asks for a unique, descriptive User-Agent, ideally with your username.")

        form = QFormLayout()
        form.addRow("Client ID", self.client_id)
        form.addRow("Client secret", self.client_secret)
        form.addRow("User-Agent", self.user_agent)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)
        self.resize(520, 0)

    def accept(self) -> None:
        self.settings.setValue("reddit/client_id", self.client_id.text().strip())
        self.settings.setValue("reddit/client_secret", self.client_secret.text().strip())
        self.settings.setValue("reddit/user_agent", self.user_agent.text().strip() or DEFAULT_USER_AGENT)
        super().accept()
