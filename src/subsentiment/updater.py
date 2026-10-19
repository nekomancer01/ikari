"""Check GitHub Releases for a newer version and download the installer for this platform.

Installed Windows copies update through the Setup .exe; portable copies (run from the zip) download
the zip and swap their own folder in place once the app has quit.

UI-independent so it can be tested without Qt. The public releases API needs no login.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import zipfile
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


def app_dir() -> Path:
    """The folder holding the running executable (the app folder in a packaged build)."""
    return Path(sys.executable).resolve().parent


def is_portable(platform: str = sys.platform, folder: Path | None = None,
                frozen: bool | None = None) -> bool:
    """True for a packaged Windows build run from an extracted zip rather than installed by Setup.

    Inno Setup always writes its uninstaller (unins000.exe) into the folder it installs to, so its
    absence next to ikari.exe marks a portable copy. Running from source is never "portable".
    """
    if not platform.startswith("win"):
        return False
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if not frozen:
        return False
    folder = folder or app_dir()
    return not any(folder.glob("unins*.exe"))


def pick_asset(assets: list[dict], platform: str = sys.platform,
               portable: bool = False) -> dict | None:
    """Choose the release file to install on `platform` (a sys.platform value).

    portable: a Windows copy run from the zip, which updates from the zip, never the installer.
    """
    def find(pred):
        return next((a for a in assets if pred(a.get("name", "").lower())), None)

    def windows_zip(n):
        return n.endswith(".zip") and "windows" in n

    if platform.startswith("win"):
        if portable:
            return find(windows_zip)
        return find(lambda n: n.endswith(".exe") and "setup" in n) or find(windows_zip)
    if platform == "darwin":
        return find(lambda n: n.endswith(".dmg"))
    return find(lambda n: n.endswith(".appimage")) or find(lambda n: n.endswith(".tar.gz"))


def latest_release(session: requests.Session | None = None, platform: str = sys.platform,
                   portable: bool | None = None) -> Release:
    if portable is None:
        portable = is_portable(platform)
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
    asset = pick_asset(data.get("assets") or [], platform, portable)
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


STAGING = ".ikari-update"  # inside the app folder, so the swap is a same-drive rename
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._ -]+$")

# Runs from <app>\.ikari-update\. Every path is relative to the app folder, so nothing from the
# user's file system is ever spliced into the script. {pid}, {exe} and {items} are filled in below.
_SWAP_SCRIPT = r"""@echo off
setlocal
cd /d "%~dp0.."
set tries=0
:wait
tasklist /FI "PID eq {pid}" /NH 2>nul | find " {pid} " >nul || goto swap
set /a tries+=1
if %tries% geq 120 goto cleanup
ping -n 2 127.0.0.1 >nul
goto wait
:swap
for %%i in ({items}) do (
  if exist "%%~i.old-update" rmdir /s /q "%%~i.old-update" 2>nul & del /f /q "%%~i.old-update" 2>nul
  if exist "%%~i" move /y "%%~i" "%%~i.old-update" >nul || goto rollback
)
for %%i in ({items}) do (
  move /y "{staging}\new\%%~i" "%%~i" >nul || goto rollback
)
for %%i in ({items}) do (
  if exist "%%~i.old-update" rmdir /s /q "%%~i.old-update" 2>nul & del /f /q "%%~i.old-update" 2>nul
)
goto done
:rollback
for %%i in ({items}) do (
  if exist "%%~i.old-update" (
    if exist "%%~i" rmdir /s /q "%%~i" 2>nul & del /f /q "%%~i" 2>nul
    move /y "%%~i.old-update" "%%~i" >nul
  )
)
:done
start "" "{exe}"
:cleanup
(goto) 2>nul & rmdir /s /q "{staging}"
"""


def stage_portable_update(zip_path: Path, folder: Path, pid: int, exe_name: str) -> Path:
    """Unpack a windows zip next to the running app and write the script that swaps it in.

    The zip holds one top-level ikari/ folder. Only what it contains is replaced, so anything else
    in the app folder stays; user data lives in the OS app-data folder and is never touched.
    Returns the script to run once this process is about to exit.
    """
    if not _SAFE_NAME.match(exe_name):
        raise UpdateError(f"Unexpected executable name: {exe_name!r}")
    staging = folder / STAGING
    _rmtree(staging)
    try:
        new = _unpack(zip_path, staging)
        items = sorted(p.name for p in new.iterdir())
        if exe_name not in items or STAGING in items or not all(_SAFE_NAME.match(n) for n in items):
            raise UpdateError("The update archive does not look like an ikari build.")
    except BaseException:
        _rmtree(staging)
        raise

    script = staging / "apply-update.cmd"
    script.write_text(_SWAP_SCRIPT.format(
        pid=int(pid), exe=exe_name, staging=STAGING,
        items=" ".join(f'"{n}"' for n in items)).replace("\n", "\r\n"), encoding="ascii")
    return script


def _unpack(zip_path: Path, staging: Path) -> Path:
    """Extract into staging/new and return it, minus the zip's single top-level folder."""
    unpacked = staging / "unpacked"
    try:
        unpacked.mkdir(parents=True)
        with zipfile.ZipFile(zip_path) as zf:
            for name in zf.namelist():
                parts = Path(name.replace("\\", "/")).parts
                if not parts or name.startswith(("/", "\\")) or ".." in parts or ":" in name:
                    raise UpdateError(f"Unsafe path in update archive: {name!r}")
            zf.extractall(unpacked)
        tops = list(unpacked.iterdir())
        root = tops[0] if len(tops) == 1 and tops[0].is_dir() else unpacked
        return root.rename(staging / "new")
    except (OSError, zipfile.BadZipFile) as e:
        raise UpdateError(f"Could not unpack the update: {e}") from e


def _rmtree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def launch_installer(path: Path, platform: str = sys.platform) -> bool:
    """Start the downloaded installer. True if the app should now quit so it can be replaced."""
    if platform.startswith("win") and path.suffix.lower() == ".zip" and is_portable(platform):
        exe = Path(sys.executable)
        script = stage_portable_update(path, exe.resolve().parent, os.getpid(), exe.name)
        # A hidden console for the script and everything it runs. The reset variable makes the
        # relaunched ikari.exe start fresh instead of treating itself as a child of this process.
        subprocess.Popen(["cmd.exe", "/d", "/c", str(script)], close_fds=True,
                         cwd=str(script.parent),
                         env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"},
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                         | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return True
    if platform.startswith("win") and path.suffix.lower() == ".exe":
        # The Inno Setup wizard closes this app if it is still running, then installs over it.
        subprocess.Popen([str(path), "/CLOSEAPPLICATIONS"], close_fds=True,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
        return True
    return False  # zip/dmg/AppImage: the caller reveals the file for the user
