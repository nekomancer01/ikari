import sys

import pytest

from subsentiment import updater
from subsentiment.updater import Release, UpdateError, is_newer, parse_version, pick_asset

ASSETS = [{"name": n, "browser_download_url": f"https://x/{n}", "size": 4} for n in (
    "ikari-windows-portable.zip", "ikari-Setup-0.3.0.exe",
    "ikari.dmg", "ikari-x86_64.AppImage", "ikari-linux.tar.gz")]


def test_parse_and_compare():
    assert parse_version("v1.2.3") == (1, 2, 3)
    assert parse_version("1.0") == parse_version("1.0.0")
    assert is_newer("v0.10.0", "0.9.0")
    assert not is_newer("0.1.0", "0.1.0")
    assert not is_newer("0.0.9", "0.1.0")
    with pytest.raises(ValueError):
        parse_version("nightly")


def test_pick_asset_per_platform():
    assert pick_asset(ASSETS, "win32")["name"].endswith("0.3.0.exe")
    assert pick_asset(ASSETS, "darwin")["name"].endswith(".dmg")
    assert pick_asset(ASSETS, "linux")["name"].endswith(".AppImage")
    assert pick_asset([ASSETS[0]], "win32")["name"].endswith(".zip")
    assert pick_asset([], "linux") is None


class FakeResp:
    def __init__(self, status=200, json_data=None, body=b"", headers=None):
        self.status_code, self._json, self.body, self.headers = status, json_data, body, headers or {}
        self.ok = status < 400

    def json(self):
        return self._json

    def iter_content(self, n):
        yield self.body[:2]
        yield self.body[2:]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class FakeSession:
    def __init__(self, resp):
        self.resp = resp

    def get(self, *a, **k):
        return self.resp


def test_latest_release():
    data = {"tag_name": "v0.3.0", "body": "notes", "html_url": "https://r", "assets": ASSETS}
    rel = updater.latest_release(FakeSession(FakeResp(json_data=data)), "win32")
    assert rel.version == "0.3.0" and rel.asset_name.endswith(".exe")


@pytest.mark.parametrize("status", [404, 403, 500])
def test_latest_release_errors(status):
    with pytest.raises(UpdateError):
        updater.latest_release(FakeSession(FakeResp(status)))


def test_download(tmp_path):
    rel = Release("1", "v1", "", "", "../evil.exe", "https://x", 4)
    seen = []
    p = updater.download(rel, tmp_path, lambda d, t: seen.append((d, t)),
                         session=FakeSession(FakeResp(body=b"abcd", headers={"Content-Length": "4"})))
    assert p == tmp_path / "evil.exe" and p.read_bytes() == b"abcd" and seen[-1] == (4, 4)
    assert not list(tmp_path.glob("*.part"))


def test_download_incomplete_cleans_up(tmp_path):
    rel = Release("1", "v1", "", "", "a.exe", "https://x", 9)
    with pytest.raises(UpdateError):
        updater.download(rel, tmp_path, session=FakeSession(FakeResp(body=b"abcd", headers={"Content-Length": "9"})))
    assert not list(tmp_path.iterdir())


RELEASE_ASSETS = [{"name": n, "browser_download_url": f"https://x/{n}", "size": 4} for n in (
    "ikari-0.3.1-windows-x64.zip", "ikari-Setup-0.3.1.exe")]


def test_pick_asset_portable_windows_takes_the_zip():
    assert pick_asset(RELEASE_ASSETS, "win32", portable=True)["name"] == "ikari-0.3.1-windows-x64.zip"
    assert pick_asset(RELEASE_ASSETS, "win32", portable=False)["name"] == "ikari-Setup-0.3.1.exe"
    # A portable copy never falls back to the installer.
    assert pick_asset([RELEASE_ASSETS[1]], "win32", portable=True) is None
    assert pick_asset(ASSETS, "darwin", portable=True)["name"].endswith(".dmg")


def test_is_portable(tmp_path):
    (tmp_path / "ikari.exe").write_bytes(b"")
    assert updater.is_portable("win32", tmp_path, frozen=True)
    (tmp_path / "unins000.exe").write_bytes(b"")
    assert not updater.is_portable("win32", tmp_path, frozen=True)
    assert not updater.is_portable("win32", tmp_path / "x", frozen=False)  # running from source
    assert not updater.is_portable("darwin", tmp_path / "x", frozen=True)


def test_latest_release_portable_picks_zip():
    data = {"tag_name": "v0.3.1", "body": "", "html_url": "https://r", "assets": RELEASE_ASSETS}
    rel = updater.latest_release(FakeSession(FakeResp(json_data=data)), "win32", portable=True)
    assert rel.asset_name == "ikari-0.3.1-windows-x64.zip"
    rel = updater.latest_release(FakeSession(FakeResp(json_data=data)), "win32", portable=False)
    assert rel.asset_name == "ikari-Setup-0.3.1.exe"


def make_zip(path, files):
    import zipfile
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def test_stage_portable_update(tmp_path):
    app = tmp_path / "ikari"
    (app / "_internal").mkdir(parents=True)
    (app / "ikari.exe").write_bytes(b"old")
    z = make_zip(tmp_path / "u.zip", {"ikari/ikari.exe": b"new", "ikari/_internal/base.dll": b"x"})
    script = updater.stage_portable_update(z, app, 4321, "ikari.exe")
    staged = app / updater.STAGING / "new"
    assert (staged / "ikari.exe").read_bytes() == b"new"
    text = script.read_bytes().decode("ascii")
    assert "\r\n" in text and "PID eq 4321" in text and '"_internal" "ikari.exe"' in text
    assert str(tmp_path) not in text  # only relative paths reach the script
    assert (app / "ikari.exe").read_bytes() == b"old"  # nothing swapped until the script runs


@pytest.mark.parametrize("files", [
    {"ikari/other.exe": b"x"},                         # no ikari.exe
    {"ikari/ikari.exe": b"x", "../evil.txt": b"x"},    # path traversal
    {"ikari/ikari.exe": b"x", "ikari/a&b.dll": b"x"},  # name unsafe for the script
])
def test_stage_portable_update_rejects_bad_archives(tmp_path, files):
    app = tmp_path / "app"
    app.mkdir()
    z = make_zip(tmp_path / "u.zip", files)
    with pytest.raises(UpdateError):
        updater.stage_portable_update(z, app, 1, "ikari.exe")
    assert not (app / updater.STAGING).exists()


def test_stage_portable_update_bad_zip(tmp_path):
    (tmp_path / "u.zip").write_bytes(b"not a zip")
    with pytest.raises(UpdateError):
        updater.stage_portable_update(tmp_path / "u.zip", tmp_path, 1, "ikari.exe")


@pytest.mark.skipif(sys.platform != "win32", reason="runs the Windows swap script")
def test_portable_swap_script_end_to_end(tmp_path):
    import shutil
    import subprocess
    import time
    exe = shutil.which("whoami.exe")  # a harmless console program stands in for ikari.exe
    app = tmp_path / "ikari"
    (app / "_internal").mkdir(parents=True)
    (app / "_internal" / "old.dll").write_bytes(b"old")
    shutil.copy(exe, app / "ikari.exe")
    (app / "notes.txt").write_text("keep me")
    z = tmp_path / "u.zip"
    with __import__("zipfile").ZipFile(z, "w") as zf:
        zf.write(exe, "ikari/ikari.exe")
        zf.writestr("ikari/_internal/new.dll", b"new")
    alive = subprocess.Popen(["ping", "-n", "3", "127.0.0.1"], stdout=subprocess.DEVNULL)
    script = updater.stage_portable_update(z, app, alive.pid, "ikari.exe")
    started = time.monotonic()
    subprocess.run(["cmd.exe", "/d", "/c", str(script)], cwd=str(app), timeout=60)
    for _ in range(50):  # the script deletes its own folder as it exits
        if not (app / updater.STAGING).exists():
            break
        time.sleep(0.2)
    assert alive.poll() is not None and time.monotonic() - started > 1  # waited for the app to exit
    assert (app / "_internal" / "new.dll").exists()
    assert not (app / "_internal" / "old.dll").exists()
    assert (app / "notes.txt").read_text() == "keep me"
    assert not list(app.glob("*.old-update"))
    assert not (app / updater.STAGING).exists()
