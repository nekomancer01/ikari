"""Reddit secret storage: OS keychain first, QSettings only when no keychain works."""
import os

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.backends import fail
from keyring.errors import PasswordDeleteError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

from subsentiment.gui import settings_dialog as sd  # noqa: E402


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        self.data = {}
        self.fail_set = False

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        if self.fail_set:
            raise keyring.errors.PasswordSetError("locked")
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        if (service, username) not in self.data:
            raise PasswordDeleteError("missing")
        del self.data[(service, username)]


@pytest.fixture
def settings(tmp_path):
    return QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat)


@pytest.fixture
def kr():
    previous = keyring.get_keyring()
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


@pytest.fixture
def no_kr():
    previous = keyring.get_keyring()
    keyring.set_keyring(fail.Keyring())
    yield
    keyring.set_keyring(previous)


def test_secret_goes_to_keychain_not_settings(settings, kr):
    assert sd._set_secret(settings, "cid", "s3cret") is True
    assert kr.data == {(sd.KEYRING_SERVICE, "cid"): "s3cret"}
    assert not settings.contains("reddit/client_secret")
    assert sd._get_secret(settings, "cid") == "s3cret"


def test_fail_backend_means_no_keychain_and_plain_settings_fallback(settings, no_kr):
    assert sd._keyring() is None
    assert sd._set_secret(settings, "cid", "s3cret") is False
    assert sd._get_secret(settings, "cid") == "s3cret"
    assert settings.value("reddit/client_secret", "", str) == "s3cret"


def test_migrates_from_settings_to_keychain_and_scrubs_plaintext(settings, kr):
    settings.setValue("reddit/client_secret", "old-plain")
    assert sd._get_secret(settings, "cid") == "old-plain"  # readable before it is re-saved
    sd._set_secret(settings, "cid", "old-plain")
    assert not settings.contains("reddit/client_secret")


def test_clearing_the_secret_deletes_it_and_tolerates_a_missing_entry(settings, kr):
    sd._set_secret(settings, "cid", "x")
    assert sd._set_secret(settings, "cid", "") is True
    assert kr.data == {}
    assert sd._set_secret(settings, "cid", "") is True  # nothing left to delete: no error


def test_keychain_failure_falls_back_to_settings_and_the_new_value_wins(settings, kr):
    sd._set_secret(settings, "cid", "old")
    kr.fail_set = True
    assert sd._set_secret(settings, "cid", "new") is False
    assert sd._get_secret(settings, "cid") == "new"  # not the stale keychain entry


def test_load_returns_defaults_and_stored_values(settings, kr):
    assert sd.load(settings) == {"client_id": "", "client_secret": "", "user_agent": sd.DEFAULT_USER_AGENT}
    settings.setValue("reddit/client_id", "cid")
    settings.setValue("reddit/user_agent", "me/1.0")
    sd._set_secret(settings, "cid", "s3cret")
    assert sd.load(settings) == {"client_id": "cid", "client_secret": "s3cret", "user_agent": "me/1.0"}


def test_dialog_saves_to_keychain_and_drops_the_old_apps_secret(settings, kr):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    settings.setValue("reddit/client_id", "old")
    sd._set_secret(settings, "old", "old-secret")
    dlg = sd.SettingsDialog(settings)
    dlg.client_id.setText(" new ")
    dlg.client_secret.setText(" new-secret ")
    dlg.user_agent.setText("")
    dlg.accept()
    assert kr.data == {(sd.KEYRING_SERVICE, "new"): "new-secret"}
    assert sd.load(settings) == {"client_id": "new", "client_secret": "new-secret",
                                 "user_agent": sd.DEFAULT_USER_AGENT}
