"""Check GitHub Releases for a newer version and download the installer for this platform.

UI-independent so it can be tested without Qt. The public releases API needs no login.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from subsentiment import __version__

REPO = "nekomancer01/ikari"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
TIMEOUT = 15


class UpdateError(Exception):
    pass


@dataclass
class Release:
    version: str
    tag: str
    notes: str
    page_url: str
    asset_name: str | None  # None when the release has no file for this platform
    asset_url: str | None
    asset_size: int = 0


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.2.3' -> (1, 2, 3). Pre-release suffixes are ignored."""
    m = re.match(r"\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        raise ValueError(f"Not a version: {text!r}")
    nums = [int(p) for p in m.group(1).split(".")]
    while len(nums) > 1 and nums[-1] == 0:  # 1.0 == 1.0.0
        nums.pop()
    return tuple(nums)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def pick_asset(assets: list[dict], platform: str = sys.platform) -> dict | None:
    """Choose the release file to install on `platform` (a sys.platform value)."""
    def find(pred):
        return next((a for a in assets if pred(a.get("name", "").lower())), None)

    if platform.startswith("win"):
        return (find(lambda n: n.endswith(".exe") and "setup" in n)
                or find(lambda n: n.endswith(".zip") and "windows" in n))
    if platform == "darwin":
        return find(lambda n: n.endswith(".dmg"))
    return find(lambda n: n.endswith(".appimage")) or find(lambda n: n.endswith(".tar.gz"))


def latest_release(session: requests.Session | None = None, platform: str = sys.platform) -> Release:
    s = session or requests
    try:
        r = s.get(API_URL, timeout=TIMEOUT, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"ikari/{__version__}",
        })
    except requests.RequestException as e:
        raise UpdateError(f"Could not reach GitHub: {e}") from e
    if r.status_code == 404:
        raise UpdateError("No releases have been published yet.")
    if r.status_code in (403, 429):
        raise UpdateError("GitHub rate limit reached. Try again later.")
    if not r.ok:
        raise UpdateError(f"GitHub returned HTTP {r.status_code}.")
    data = r.json()
    tag = data.get("tag_name") or ""
    try:
        parse_version(tag)
    except ValueError as e:
        raise UpdateError(f"Latest release has an unreadable version tag: {tag!r}") from e
    asset = pick_asset(data.get("assets") or [], platform)
    return Release(
        version=tag.lstrip("vV"), tag=tag, notes=data.get("body") or "",
        page_url=data.get("html_url") or f"https://github.com/{REPO}/releases",
        asset_name=asset["name"] if asset else None,
        asset_url=asset["browser_download_url"] if asset else None,
        asset_size=int(asset.get("size") or 0) if asset else 0)


def download(release: Release, dest_dir: Path,
             progress: Callable[[int, int], None] | None = None,
             should_stop: Callable[[], bool] = lambda: False,
             session: requests.Session | None = None) -> Path:
    """Download the release asset into dest_dir and return its path."""
    if not release.asset_url or not release.asset_name:
        raise UpdateError("This release has no download for your platform.")
    dest_dir.mkdir(parents=True, exist_ok=True)
    final = dest_dir / Path(release.asset_name).name  # basename only: never trust a path from the API
    part = final.with_name(final.name + ".part")
    s = session or requests
    try:
        with s.get(release.asset_url, stream=True, timeout=TIMEOUT,
                   headers={"User-Agent": f"ikari/{__version__}"}) as r:
            if not r.ok:
                raise UpdateError(f"Download failed: HTTP {r.status_code}.")
            total = int(r.headers.get("Content-Length") or release.asset_size or 0)
            done = 0
            with open(part, "wb") as f:
                for chunk in r.iter_content(1 << 16):
                    if should_stop():
                        raise UpdateError("Download cancelled.")
                    f.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
        if total and done != total:
            raise UpdateError("Download was incomplete.")
        os.replace(part, final)
    except requests.RequestException as e:
        raise UpdateError(f"Download failed: {e}") from e
    finally:
        part.unlink(missing_ok=True)
    return final


def launch_installer(path: Path, platform: str = sys.platform) -> bool:
    """Start the downloaded installer. True if the app should now quit so it can be replaced."""
    if platform.startswith("win") and path.suffix.lower() == ".exe":
        # The Inno Setup wizard closes this app if it is still running, then installs over it.
        subprocess.Popen([str(path), "/CLOSEAPPLICATIONS"], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return True
    return False  # zip/dmg/AppImage: the caller reveals the file for the user
