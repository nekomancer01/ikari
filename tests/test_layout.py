"""Layout checks on every common screen and scaling: nothing cut off, squashed, overlapping or out of line.

Renders the real main window and Download coverage window offscreen with tools/ui_matrix.py (16:9, 16:10 and
MacBook sizes at their usual scalings; restored, maximized and dragged down to the minimum) and fails with
every problem it finds, one test per screen. To see what a failure looks like:

    python tools/ui_matrix.py --only win-1920x1200-150 --out shots/
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ui_matrix.py"
_spec = importlib.util.spec_from_file_location("ui_matrix", TOOL)
ui_matrix = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ui_matrix)
TAGS = [row[0] for row in ui_matrix.screens()]
# One render covers every screen, so the whole matrix stays on one test worker (tools/check.py uses loadgroup).
pytestmark = pytest.mark.xdist_group("layout")


@pytest.fixture(scope="module")
def matrix(tmp_path_factory):
    out = tmp_path_factory.mktemp("ui-matrix")
    proc = subprocess.run([sys.executable, str(TOOL), "--out", str(out), "--no-shots"],
                          capture_output=True, text=True, timeout=1800, check=False)
    report = out / "report.json"
    assert report.exists(), f"the layout matrix didn't run:\n{proc.stdout[-2000:]}\n{proc.stderr[-4000:]}"
    return {r["tag"]: r for r in json.loads(report.read_text(encoding="utf-8"))}, proc.stderr


@pytest.mark.parametrize("tag", TAGS)
def test_layout(matrix, tag):
    reports, stderr = matrix
    assert tag in reports, f"{tag} failed to render:\n{stderr[-4000:]}"
    problems = [f"{r['state']} at {r['size'][0]}x{r['size'][1]}: {p}"
                for r in reports[tag]["results"] for p in r["problems"]]
    assert not problems, f"{len(problems)} layout problems on {tag}:\n" + "\n".join(problems)
