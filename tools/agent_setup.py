"""Session-start setup for coding agents, run by the hook in .claude/settings.json.

Turns on .githooks/pre-push, so every push first runs tools/check.py. In a cloud session (a fresh clone with
nothing installed) it also installs what check.py needs; on a person's own machine it only says what's missing.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Installed as plain packages, not `pip install -e .`: check.py imports this checkout's src directly, so an
# editable install pointing at another checkout can't swap in someone else's code.
NEEDED = {"numpy": "numpy>=1.26", "PySide6": "PySide6>=6.7", "requests": "requests>=2.31", "vaderSentiment": "vaderSentiment>=3.3.2",
          "zstandard": "zstandard>=0.22", "pytest": "pytest>=8", "xdist": "pytest-xdist>=3", "ruff": "ruff>=0.15", "hypothesis": "hypothesis>=6.100",
          "PyInstaller": "pyinstaller"}  # tests/test_packaging.py reads what the spec bundles
QT_LIBRARIES = ["libegl1", "libgl1", "libxkbcommon0", "libfontconfig1", "libdbus-1-3"]


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()


def turn_on_pre_push() -> None:
    current = git("config", "--local", "core.hooksPath")
    if current and current != ".githooks":
        print(f"core.hooksPath is already {current}; leaving it. The pre-push check is not on.")
        return
    if not current:
        git("config", "--local", "core.hooksPath", ".githooks")
    print("Pre-push check on: every git push runs tools/check.py first.")


def missing() -> list[str]:
    return [spec for module, spec in NEEDED.items() if importlib.util.find_spec(module) is None]


def qt_loads() -> bool:
    probe = "from PySide6.QtWidgets import QApplication; QApplication([])"
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    return subprocess.run([sys.executable, "-c", probe], env=env, capture_output=True).returncode == 0


def install_qt_libraries() -> None:
    """A bare Linux container lacks the system libraries Qt loads (libEGL first), so every GUI test errors."""
    if not sys.platform.startswith("linux") or qt_loads():
        return
    print("Installing the system libraries Qt needs")
    apt = ["apt-get", "install", "-y", "-qq", *QT_LIBRARIES]
    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    try:
        if subprocess.run(apt, **quiet).returncode:
            subprocess.run(["apt-get", "update", "-qq"], **quiet)
            subprocess.run(apt, **quiet)
    except OSError:
        pass
    if not qt_loads():
        print("Qt still can't start, so the GUI tests in tools/check.py will fail.")


def main() -> int:
    turn_on_pre_push()
    need = missing()
    remote = os.environ.get("CLAUDE_CODE_REMOTE") == "true"
    if need and not remote:
        print("tools/check.py needs these installed first: " + ", ".join(need))
        return 0
    if need:
        print("Installing test dependencies: " + ", ".join(need))
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *need], cwd=ROOT)
        if result.returncode:
            print("Installing them failed; tools/check.py (and so git push) will fail until they're installed.")
            return 0
    if remote:
        install_qt_libraries()
    return 0


if __name__ == "__main__":
    sys.exit(main())
