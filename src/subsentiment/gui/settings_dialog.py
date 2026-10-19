from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QVBoxLayout,
)

from subsentiment.reddit import DEFAULT_USER_AGENT

KEYRING_SERVICE = "Subreddit Sentiment"  # legacy name, kept so saved Reddit secrets survive the rename


def _keyring():
    """The OS secret store (Windows Credential Manager, macOS Keychain, Secret Service), or None."""
    try:
        import keyring
        from keyring.backends import fail
        if isinstance(keyring.get_keyring(), fail.Keyring):
            return None
        return keyring
    except Exception:
        return None


def _get_secret(settings: QSettings, client_id: str) -> str:
    # A plaintext value means the keychain write failed (or predates it) and is newer than any keychain entry:
    # saving to the keychain removes the plaintext copy.
    plain = settings.value("reddit/client_secret", "", str)
    if plain:
        return plain
    kr = _keyring()
    if kr and client_id:
        try:
            secret = kr.get_password(KEYRING_SERVICE, client_id)
            if secret:
                return secret
        except Exception:
            pass
    return ""


def _set_secret(settings: QSettings, client_id: str, secret: str) -> bool:
    """Store the secret in the OS store if possible. Returns True if it was stored securely."""
    kr = _keyring()
    if kr and client_id:
        try:
            if secret:
                kr.set_password(KEYRING_SERVICE, client_id, secret)
            else:
                try:
                    kr.delete_password(KEYRING_SERVICE, client_id)
                except Exception:
                    pass
            settings.remove("reddit/client_secret")
            return True
        except Exception:
            pass
    settings.setValue("reddit/client_secret", secret)
    return False


def load(settings: QSettings) -> dict:
    client_id = settings.value("reddit/client_id", "", str)
    return {
        "client_id": client_id,
        "client_secret": _get_secret(settings, client_id),
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
            + ("<i>The secret is kept in your system's credential store "
               "(Windows Credential Manager on Windows).</i>" if _keyring() else
               "<i>No system credential store was found, so the secret is stored unencrypted "
               "in this app's settings.</i>"))
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
        client_id = self.client_id.text().strip()
        old_id = self.settings.value("reddit/client_id", "", str)
        if old_id and old_id != client_id:
            _set_secret(self.settings, old_id, "")  # don't leave the old app's secret behind
        self.settings.setValue("reddit/client_id", client_id)
        _set_secret(self.settings, client_id, self.client_secret.text().strip())
        self.settings.setValue("reddit/user_agent", self.user_agent.text().strip() or DEFAULT_USER_AGENT)
        super().accept()
