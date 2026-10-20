"""Runs exactly what the required "Windows x64" PR check runs: the whole test suite, e2e and layout included.

Run from the repo root before every push:  python tools/check.py [extra pytest args]
It first says whether the branch is behind origin/main (merge main in, then run it again, since main only
takes branches that are up to date). On Linux and in containers Qt is made to render offscreen.
To run it on every push automatically:     git config core.hooksPath .githooks
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def behind_main() -> int | None:
    """Commits on origin/main this branch doesn't have, or None when git can't tell (no remote, offline)."""
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin", "main"], cwd=ROOT, check=True, timeout=60,
                       capture_output=True)
        out = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=ROOT, check=True,
                             capture_output=True, text=True).stdout
        return int(out.strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def main() -> int:
    if "--no-fetch" in sys.argv:
        sys.argv.remove("--no-fetch")
    else:
        behind = behind_main()
        if behind:
            print(f"This branch is {behind} commit(s) behind origin/main. Merge it in "
                  "(git merge origin/main) and run this again.", file=sys.stderr)
            return 1
    env = dict(os.environ)
    if sys.platform.startswith("linux") and "QT_QPA_PLATFORM" not in env:
        env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], cwd=ROOT, env=env).returncode


if __name__ == "__main__":
    sys.exit(main())
