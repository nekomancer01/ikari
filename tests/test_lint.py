"""Ruff over the app, its tests and tools, with the bug-finding rules in pyproject.toml ([tool.ruff])."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_finds_nothing():
    run = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-cache", "src", "tests", "tools"], cwd=ROOT,
                         capture_output=True, text=True)
    assert "No module named ruff" not in run.stderr, "ruff isn't installed: pip install -e .[dev]"
    assert run.returncode == 0, run.stdout + run.stderr
