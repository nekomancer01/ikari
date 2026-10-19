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
