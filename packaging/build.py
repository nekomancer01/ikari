"""Build ikari for the OS this runs on. Run from anywhere, inside an environment
with the app and PyInstaller installed:

    pip install . pyinstaller
    python packaging/build.py

Output in dist/:
  Windows: ikari/ (the app folder), ikari-<v>-windows-x64.zip and,
           if Inno Setup 6 is installed, ikari-Setup-<v>.exe
  macOS:   ikari.app (ad-hoc signed) and ikari-<v>-macos-<arch>.dmg
  Linux:   ikari-<v>-linux-x86_64.AppImage and ikari-<v>-linux-x86_64.tar.gz
           (the app folder plus install.sh). --no-appimage skips the AppImage, which needs
           appimagetool (found on PATH, in $APPIMAGETOOL, or downloaded).
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
ASSETS = ROOT / "src" / "subsentiment" / "gui" / "assets"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_ID = "io.github.nekomancer01.ikari"
APPIMAGETOOL_URL = ("https://github.com/AppImage/appimagetool/releases/download/continuous/"
                    "appimagetool-x86_64.AppImage")


def version() -> str:
    text = (ROOT / "src" / "subsentiment" / "__init__.py").read_text(encoding="utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def run(*cmd, **kwargs) -> None:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    subprocess.run([str(c) for c in cmd], check=True, **kwargs)


def pyinstaller() -> None:
    run(sys.executable, "-m", "PyInstaller", PACKAGING / "subsentiment.spec", "--noconfirm",
        "--distpath", DIST, "--workpath", BUILD)


def check_no_dev_files() -> None:
    """Fail the build if test/training tooling ended up in the app folder."""
    banned = {"pytest", "_pytest", "sklearn", "scipy", "numpy", "tools", "tests"}
    root = DIST / "ikari.app" if sys.platform == "darwin" else DIST / "ikari"
    found = sorted({p.name for p in root.rglob("*") if p.name in banned})
    if found:
        raise SystemExit(f"dev-only files in the package: {', '.join(found)}")


def build_windows(v: str) -> None:
    app = DIST / "ikari"
    zip_base = DIST / f"ikari-{v}-windows-x64"
    shutil.make_archive(str(zip_base), "zip", DIST, app.name)
    print("wrote", zip_base.with_suffix(".zip"))

    iscc = shutil.which("iscc") or os.path.join(os.environ.get("ProgramFiles(x86)", ""),
                                                "Inno Setup 6", "ISCC.exe")
    if os.path.exists(iscc):
        run(iscc, f"/DAppVersion={v}", PACKAGING / "installer.iss")
    else:
        print("Inno Setup 6 not found; skipping the installer (https://jrsoftware.org/isinfo.php).")


def build_macos(v: str) -> None:
    app = DIST / "ikari.app"
    # Ad-hoc signature: not trusted by Gatekeeper, but Apple Silicon refuses unsigned code entirely.
    run("codesign", "--force", "--deep", "--sign", "-", app)
    run("codesign", "--verify", "--deep", "--strict", app)

    stage = BUILD / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    run("ditto", app, stage / app.name)  # ditto keeps the bundle's symlinks and signature intact
    (stage / "Applications").symlink_to("/Applications")
    arch = platform.machine()  # arm64 or x86_64
    dmg = DIST / f"ikari-{v}-macos-{arch}.dmg"
    dmg.unlink(missing_ok=True)
    run("hdiutil", "create", "-volname", "ikari", "-srcfolder", stage,
        "-ov", "-format", "UDZO", dmg)


def appimagetool() -> Path:
    found = os.environ.get("APPIMAGETOOL") or shutil.which("appimagetool")
    if found:
        return Path(found)
    tool = BUILD / "appimagetool-x86_64.AppImage"
    if not tool.exists():
        print("downloading", APPIMAGETOOL_URL, flush=True)
        BUILD.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(APPIMAGETOOL_URL, tool)
        tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    return tool


def build_linux(v: str, make_appimage: bool) -> None:
    app = DIST / "ikari"
    linux = PACKAGING / "linux"
    name = f"ikari-{v}-linux-x86_64"

    # Portable tarball: the app folder plus a per-user installer.
    stage = BUILD / "tarball" / name
    shutil.rmtree(stage.parent, ignore_errors=True)
    shutil.copytree(app, stage / app.name, symlinks=True)
    shutil.copy2(linux / "install.sh", stage)
    shutil.copy2(linux / f"{APP_ID}.desktop", stage)
    shutil.copy2(ASSETS / "app-512.png", stage / f"{APP_ID}.png")
    tarball = DIST / f"{name}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(stage, arcname=name)
    print("wrote", tarball)

    if not make_appimage:
        return
    appdir = BUILD / "AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(app, appdir / "usr" / "lib" / app.name, symlinks=True)
    shutil.copy2(linux / "AppRun", appdir / "AppRun")
    shutil.copy2(linux / f"{APP_ID}.desktop", appdir)
    shutil.copy2(ASSETS / "app-512.png", appdir / f"{APP_ID}.png")
    icons = appdir / "usr" / "share" / "icons" / "hicolor" / "512x512" / "apps"
    icons.mkdir(parents=True)
    shutil.copy2(ASSETS / "app-512.png", icons / f"{APP_ID}.png")
    (appdir / ".DirIcon").symlink_to(f"{APP_ID}.png")

    out = DIST / f"{name}.AppImage"
    # Extract-and-run lets appimagetool (itself an AppImage) work without FUSE, as on CI runners.
    env = {**os.environ, "ARCH": "x86_64", "APPIMAGE_EXTRACT_AND_RUN": "1"}
    run(appimagetool(), appdir, out, env=env)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--no-appimage", action="store_true", help="Linux: only build the tarball")
    args = parser.parse_args()

    v = version()
    print(f"Building ikari {v} for {sys.platform}", flush=True)
    pyinstaller()
    check_no_dev_files()
    if sys.platform == "win32":
        build_windows(v)
    elif sys.platform == "darwin":
        build_macos(v)
    else:
        build_linux(v, make_appimage=not args.no_appimage)
    print("\nDone. Packages are in", DIST)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
